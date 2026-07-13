import base64
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager import remote_validator as remote_validator_module
from manager.private_config_ssh import PrivateConfigSsh
from manager.remote_validator import (
    RemoteValidationError, RemoteValidator, bounded_subprocess_run, fixed_runtime_php,
    parse_ssh_config,
)


class FakeRunner:
    def __init__(self, responses, before=None):
        self.responses = list(responses)
        self.calls = []
        self.before = before

    def __call__(self, argv, *, input, timeout, stdout_limit, stderr_limit):
        self.calls.append((argv, input, timeout, stdout_limit, stderr_limit))
        if self.before:
            self.before(len(self.calls), argv)
        return self.responses.pop(0)


def completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RawConfigParserTest(unittest.TestCase):
    def test_accepts_exact_single_alias_and_comments(self):
        data = b"# comment\nHost safe-alias # ordinary comment\n  HostName example.invalid\n"
        self.assertEqual("safe-alias", parse_ssh_config(data, "safe-alias"))

    def test_rejects_dynamic_and_ambiguous_directives_case_insensitively(self):
        cases = [
            b"Include other\nHost safe-alias\n",
            b"Host other\n  mAtCh exec true\nHost safe-alias\n",
            b"Host safe-alias\n  HOSTKEYALIAS alternate\n",
            b"Host safe-alias other\n",
            b"Host safe-*\n",
            b"Host !bad safe-alias\n",
            b"Host \"safe-alias\"\n",
            b"Host safe-alias\\\n other\n",
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(RemoteValidationError):
                parse_ssh_config(data, "safe-alias")

    def test_rejects_ambiguous_host_patterns_anywhere_in_config(self):
        cases = [
            b"Host safe-alias\n HostName example.invalid\nHost *\n ServerAliveInterval 30\n",
            b"Host safe-alias\n HostName example.invalid\nHost !blocked\n",
            b"Host other another\nHost safe-alias\n HostName example.invalid\n",
            b"Host other?\nHost safe-alias\n HostName example.invalid\n",
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(RemoteValidationError):
                parse_ssh_config(data, "safe-alias")

    def test_comment_and_escape_state_cannot_hide_forbidden_directive(self):
        parse_ssh_config(b"Host safe-alias\n # Include ignored\n HostName x\\#y\n", "safe-alias")
        for data in (b"Host safe-alias\n Include\\ other\n", b"Host safe-alias\n Include \"unterminated\n"):
            with self.subTest(data=data), self.assertRaises(RemoteValidationError):
                parse_ssh_config(data, "safe-alias")

    def test_rejects_invalid_bytes_nul_size_and_unclosed_escape(self):
        for data in (b"Host safe-alias\x00\n", b"\xff", b"Host safe-alias\\", b"x" * (65536 + 1)):
            with self.subTest(size=len(data)), self.assertRaises(RemoteValidationError):
                parse_ssh_config(data, "safe-alias")


class RemoteValidatorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir(mode=0o700)
        self.config = self.ssh / RemoteValidator.CONFIG_BASENAME
        self.known_hosts = self.ssh / "known_hosts"
        self.config.write_text("Host safe-alias\n HostName remote.example.invalid\n", encoding="utf-8")
        self.known_hosts.write_text("remote.example.invalid ssh-ed25519 AAAA\n", encoding="utf-8")
        os.chmod(self.config, 0o600)
        os.chmod(self.known_hosts, 0o600)
        self.result = {
            "manifest": "PASS", "php_cli": "PASS", "absolute_cli_dry_run": "PASS",
            "symlinks": [], "public_root": "PASS"
        }

    def tearDown(self):
        self.temp.cleanup()

    def ssh_g(self, **overrides):
        values = {
            "hostname": "remote.example.invalid", "hostkeyalias": "none",
            "userknownhostsfile": str(self.known_hosts), "proxycommand": "none",
            "proxyjump": "none", "knownhostscommand": "none", "remotecommand": "none",
            "canonicalizehostname": "false", "clearallforwardings": "yes",
            "forwardagent": "no", "controlmaster": "false", "controlpath": "none",
            "permitlocalcommand": "no", "requesttty": "false",
        }
        values.update(overrides)
        return "".join(f"{key} {value}\n" for key, value in values.items()).encode()

    def make(self, responses, before=None):
        runner = FakeRunner(responses, before)
        return RemoteValidator("safe-alias", runner, home=self.home), runner

    def test_validation_uses_exact_argv_and_stdin_only_for_data(self):
        validator, runner = self.make([completed(self.ssh_g()), completed(json.dumps(self.result).encode())])
        manifest = {"files": [{"path": "bin/app.php", "sha256": "0" * 64, "mode": "0700"}]}

        result = validator.validate(manifest, expected_hosts=("remote.example.invalid",))

        self.assertEqual("PASS", result.manifest)
        self.assertEqual("PASS", result.php_cli)
        self.assertEqual("PASS", result.absolute_cli_dry_run)
        self.assertFalse(result.symlinks)
        inspect_argv, inspect_input, *_ = runner.calls[0]
        actual_argv, actual_input, *_ = runner.calls[1]
        expected_options = [
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=%d/.ssh/known_hosts",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
            "-o", "PermitLocalCommand=no", "-o", "RemoteCommand=none",
            "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
            "-o", "KnownHostsCommand=none", "-o", "CanonicalizeHostname=no",
            "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "RequestTTY=no", "-o", "LogLevel=ERROR",
        ]
        expected_actual = [
            "/usr/bin/ssh", "-F", str(self.config), *expected_options, "--", "safe-alias",
            "/usr/bin/php8.5 private/xserver-mail-lineworks/bootstrap/validate-release.php",
        ]
        self.assertEqual(expected_actual, actual_argv)
        self.assertEqual(["/usr/bin/ssh", "-F", str(self.config), "-G", *expected_options, "--", "safe-alias",
                          RemoteValidator.REMOTE_COMMAND], inspect_argv)
        for argv in (inspect_argv, actual_argv):
            self.assertEqual(["-F", str(self.config)], argv[1:3])
            self.assertNotIn(str(self.ssh / "config"), argv)
        self.assertIsNone(inspect_input)
        self.assertEqual(
            b'{"manifest":{"files":[{"mode":"0700","path":"bin/app.php",'
            b'"sha256":"0000000000000000000000000000000000000000000000000000000000000000"}]}}',
            actual_input,
        )
        self.assertNotIn("safe-alias", RemoteValidator.REMOTE_COMMAND)

    def test_run_trusted_reuses_full_boundary_and_preserves_stdin_only_secret(self):
        secret = b"secret-token-placeholder"
        command = "/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/bootstrap/manage-private-config.php"
        validator, runner = self.make([completed(self.ssh_g()), completed(b'{"ok":true}')])
        output = validator.run_trusted(
            command, secret, expected_hosts=["remote.example.invalid"], output_limit=131072,
        )
        self.assertEqual(b'{"ok":true}', output)
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(command, runner.calls[0][0][-1])
        self.assertEqual(command, runner.calls[1][0][-1])
        self.assertIsNone(runner.calls[0][1])
        self.assertEqual(secret, runner.calls[1][1])
        self.assertNotIn(secret.decode(), " ".join(runner.calls[1][0]))
        self.assertIn("StrictHostKeyChecking=yes", runner.calls[1][0])
        self.assertEqual((131072, 131072), runner.calls[1][3:5])

    def test_run_trusted_accepts_262144_input_but_keeps_131072_output_limit(self):
        command = "/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/bootstrap/manage-private-config.php"
        maximum = b"x" * 262144
        validator, runner = self.make([completed(self.ssh_g()), completed(b'{}')])
        self.assertEqual(b'{}', validator.run_trusted(
            command, maximum, expected_hosts=["remote.example.invalid"],
            output_limit=131072,
        ))
        self.assertEqual(maximum, runner.calls[1][1])
        self.assertEqual((131072, 131072), runner.calls[1][3:5])

        validator, runner = self.make([])
        with self.assertRaisesRegex(ValueError, "trusted SSH contract"):
            validator.run_trusted(
                command, maximum + b"x",
                expected_hosts=["remote.example.invalid"], output_limit=131072,
            )
        self.assertEqual([], runner.calls)

    def test_private_config_normal_read_and_maximum_cas_share_trusted_contract(self):
        read_response = json.dumps({
            "schema_version": 1, "config": {"safe": True}, "sha256": "a" * 64,
        }).encode()
        cas_response = json.dumps({
            "schema_version": 1, "status": "changed",
        }).encode()
        validator, runner = self.make([
            completed(self.ssh_g()), completed(read_response),
            completed(self.ssh_g()), completed(cas_response),
        ])
        client = object.__new__(PrivateConfigSsh)
        client.remote_command = (
            "/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/"
            "bootstrap/manage-private-config.php"
        )
        client.expected_hosts = ["remote.example.invalid"]
        client.validator = validator

        self.assertEqual(({"safe": True}, "a" * 64), client.read())
        desired = b"d" * 65536
        self.assertEqual(desired, client.compare_and_swap_scope_journal(
            "target-sync", b"e" * 65536, desired))
        helper_calls = [runner.calls[1], runner.calls[3]]
        self.assertLess(len(helper_calls[0][1]), 131072)
        self.assertGreater(len(helper_calls[1][1]), 131072)
        self.assertLessEqual(len(helper_calls[1][1]), 262144)
        self.assertTrue(all(call[3:5] == (131072, 131072) for call in helper_calls))

    def test_run_trusted_rejects_unexpected_host_hostile_config_and_changed_trust(self):
        command = "/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/bootstrap/manage-private-config.php"
        validator, _ = self.make([completed(self.ssh_g(hostname="unexpected.invalid"))])
        with self.assertRaises(RemoteValidationError):
            validator.run_trusted(command, b"{}", expected_hosts=["remote.example.invalid"])

        self.config.write_text("Include hostile\nHost safe-alias\n HostName remote.example.invalid\n")
        os.chmod(self.config, 0o600)
        validator, runner = self.make([])
        with self.assertRaises(RemoteValidationError):
            validator.run_trusted(command, b"{}", expected_hosts=["remote.example.invalid"])
        self.assertEqual([], runner.calls)

        self.config.write_text("Host safe-alias\n HostName remote.example.invalid\n ProxyCommand hostile\n")
        os.chmod(self.config, 0o600)
        validator, _ = self.make([completed(self.ssh_g(proxycommand="hostile"))])
        with self.assertRaises(RemoteValidationError):
            validator.run_trusted(command, b"{}", expected_hosts=["remote.example.invalid"])

        self.config.write_text("Host safe-alias\n HostName remote.example.invalid\n")
        os.chmod(self.config, 0o600)
        def replace_known_hosts(call_number, _argv):
            if call_number == 1:
                replacement = self.ssh / "replacement"
                replacement.write_bytes(self.known_hosts.read_bytes())
                os.chmod(replacement, 0o600)
                os.replace(replacement, self.known_hosts)
        validator, runner = self.make([completed(self.ssh_g())], replace_known_hosts)
        with self.assertRaises(RemoteValidationError):
            validator.run_trusted(command, b"{}", expected_hosts=["remote.example.invalid"])
        self.assertEqual(1, len(runner.calls))

    def test_fixed_runtime_details_returns_canonical_files_and_preserves_state_wrapper(self):
        entries = {
            "bootstrap": {"type": "directory", "mode": 0o700,
                          "size": 0, "sha256": None},
            "bootstrap/a.php": {"type": "file", "mode": 0o700,
                                "size": 1, "sha256": "a" * 64},
            "bootstrap/b.php": {"type": "file", "mode": 0o700,
                                "size": 1, "sha256": "b" * 64},
        }
        response = completed(
            b'{"state":"PARTIAL","present_files":["bootstrap/a.php"]}\n')
        validator, _ = self.make([completed(self.ssh_g()), response])
        self.assertEqual(
            {"state": "PARTIAL", "present_files": ("bootstrap/a.php",)},
            validator.inspect_fixed_runtime_details(
                "/home/example/private/xserver-mail-lineworks", entries,
                expected_hosts=["remote.example.invalid"],
            ),
        )
        validator, _ = self.make([completed(self.ssh_g()), response])
        self.assertEqual(
            "PARTIAL", validator.inspect_fixed_runtime(
                "/home/example/private/xserver-mail-lineworks", entries,
                expected_hosts=["remote.example.invalid"],
            ),
        )

    def test_fixed_runtime_details_rejects_noncanonical_or_inconsistent_file_inventory(self):
        entries = {
            "bootstrap": {"type": "directory", "mode": 0o700,
                          "size": 0, "sha256": None},
            "bootstrap/a.php": {"type": "file", "mode": 0o700,
                                "size": 1, "sha256": "a" * 64},
            "bootstrap/b.php": {"type": "file", "mode": 0o700,
                                "size": 1, "sha256": "b" * 64},
        }
        invalid = (
            {"state": "PARTIAL", "present_files": ["bootstrap/b.php", "bootstrap/a.php"]},
            {"state": "PARTIAL", "present_files": ["bootstrap/a.php", "bootstrap/a.php"]},
            {"state": "PARTIAL", "present_files": ["bootstrap/unknown.php"]},
            {"state": "PARTIAL", "present_files": ["bootstrap"]},
            {"state": "ABSENT", "present_files": ["bootstrap/a.php"]},
            {"state": "EXACT", "present_files": ["bootstrap/a.php"]},
        )
        for value in invalid:
            with self.subTest(value=value):
                validator, _ = self.make([
                    completed(self.ssh_g()),
                    completed((json.dumps(value, separators=(",", ":")) + "\n").encode()),
                ])
                with self.assertRaises(RemoteValidationError):
                    validator.inspect_fixed_runtime_details(
                        "/home/example/private/xserver-mail-lineworks", entries,
                        expected_hosts=["remote.example.invalid"],
                    )

    def test_fixed_helper_provision_uses_fixed_argv_and_stdin_only_payload(self):
        body = b"<?php helper-secret-placeholder;\n"
        digest = hashlib.sha256(body).hexdigest()
        validator, runner = self.make([
            completed(self.ssh_g()), completed(b'{"status":"changed"}'),
        ])

        status = validator.provision_fixed_helper(
            "/home/example/private/xserver-mail-lineworks",
            "bootstrap/manage-private-config.php", body,
            expected_sha256=digest, mode=0o700,
            expected_hosts=["remote.example.invalid"],
        )

        self.assertEqual("changed", status)
        self.assertEqual(2, len(runner.calls))
        for argv, *_rest in runner.calls:
            self.assertEqual(RemoteValidator.FIXED_HELPER_COMMAND, argv[-1])
            self.assertNotIn(body.decode(), " ".join(argv))
        self.assertIsNone(runner.calls[0][1])
        payload = json.loads(runner.calls[1][1])
        self.assertEqual({
            "schema_version": 1,
            "root": "/home/example/private/xserver-mail-lineworks",
            "relative": "bootstrap/manage-private-config.php",
            "body_base64": base64.b64encode(body).decode("ascii"),
            "sha256": digest, "mode": 0o700,
        }, payload)

    def test_fixed_helper_provision_rejects_local_path_body_hash_and_mode_attacks(self):
        body = b"<?php valid;\n"
        digest = hashlib.sha256(body).hexdigest()
        cases = (
            ("/home/example/public_html/xserver-mail-lineworks",
             "bootstrap/manage-private-config.php", body, digest, 0o700),
            ("/home/example/private/xserver-mail-lineworks/../other",
             "bootstrap/manage-private-config.php", body, digest, 0o700),
            ("/home/../private/xserver-mail-lineworks",
             "bootstrap/manage-private-config.php", body, digest, 0o700),
            ("/home/./private/xserver-mail-lineworks",
             "bootstrap/manage-private-config.php", body, digest, 0o700),
            ("/home/example/private/xserver-mail-lineworks",
             "bootstrap/other.php", body, digest, 0o700),
            ("/home/example/private/xserver-mail-lineworks",
             "bootstrap/manage-private-config.php", b"", digest, 0o700),
            ("/home/example/private/xserver-mail-lineworks",
             "bootstrap/manage-private-config.php", body, "0" * 64, 0o700),
            ("/home/example/private/xserver-mail-lineworks",
             "bootstrap/manage-private-config.php", body, digest, 0o600),
        )
        for root, relative, value, expected, mode in cases:
            with self.subTest(root=root, relative=relative, mode=mode):
                validator, runner = self.make([])
                with self.assertRaises(ValueError):
                    validator.provision_fixed_helper(
                        root, relative, value, expected_sha256=expected, mode=mode,
                        expected_hosts=["remote.example.invalid"],
                    )
                self.assertEqual([], runner.calls)

    def test_fixed_helper_provision_rechecks_trust_before_write(self):
        body = b"<?php valid;\n"
        def replace_known_hosts(call_number, _argv):
            if call_number == 1:
                replacement = self.ssh / "replacement-helper"
                replacement.write_bytes(self.known_hosts.read_bytes())
                os.chmod(replacement, 0o600)
                os.replace(replacement, self.known_hosts)
        validator, runner = self.make([completed(self.ssh_g())], replace_known_hosts)
        with self.assertRaises(RemoteValidationError):
            validator.provision_fixed_helper(
                "/home/example/private/xserver-mail-lineworks",
                "bootstrap/manage-private-config.php", body,
                expected_sha256=hashlib.sha256(body).hexdigest(), mode=0o700,
                expected_hosts=["remote.example.invalid"],
            )
        self.assertEqual(1, len(runner.calls))

    def test_accepts_repeated_benign_ssh_g_keys_but_rejects_singleton_ambiguity(self):
        validator, _ = self.make([])
        repeated = self.ssh_g() + b"identityfile ~/.ssh/id_one\nidentityfile ~/.ssh/id_two\n"
        repeated += b"sendenv LANG\nsendenv LC_*\n"
        validator._parse_g(repeated, ("remote.example.invalid",))
        with self.assertRaises(RemoteValidationError):
            validator._parse_g(self.ssh_g() + b"hostname second.invalid\n",
                               ("remote.example.invalid",))

    def test_release_validation_uses_php_schema_then_metadata_only_public_audit(self):
        release_result = {
            "schema_version": 1, "manifest": "PASS", "php_cli": "PASS",
            "autoload": "PASS", "absolute_cli_dry_run": "PASS", "symlinks": 0,
        }
        audit_result = {
            "schema_version": 3, "home_symlink": False, "home_mode": 0o700,
            "public_roots_scanned": 2, "symlinks": 0, "known_product_matches": 0,
            "untrusted_subtrees": 1, "untrusted_entries": 2,
        }
        validator, runner = self.make([
            completed(self.ssh_g()), completed(json.dumps(release_result).encode()),
            completed(json.dumps(audit_result).encode()),
        ])
        manifest = [{
            "path": "bin/app.php", "type": "file", "mode": 0o700,
            "size": 1, "sha256": "0" * 64,
        }]
        result = validator.validate_release(
            manifest, remote_root="/home/example/private/releases/release-test",
            entrypoint="bin/app.php", server_home="/home/example",
            config_path="/home/example/mail-lineworks/private/config.json",
            expected_hosts=("remote.example.invalid",),
            known_basenames=["mail-forward-command.php"],
        )
        self.assertEqual("PASS", result["autoload"])
        self.assertEqual(1, result["untrusted_subtrees"])
        self.assertEqual(2, result["untrusted_entries"])
        self.assertEqual(3, len(runner.calls))
        for argv, *_ in runner.calls:
            self.assertEqual(["-F", str(self.config)], argv[1:3])
            self.assertNotIn(str(self.ssh / "config"), argv)
        release_payload = json.loads(runner.calls[1][1])
        self.assertEqual("/home/example/private/releases/release-test", release_payload["release_path"])
        self.assertEqual(manifest, release_payload["manifest"])
        self.assertEqual("/home/example/mail-lineworks/private/config.json", release_payload["config_path"])
        self.assertEqual(
            RemoteValidator.REMOTE_COMMAND + " --audit-public-root",
            runner.calls[2][0][-1],
        )
        self.assertEqual(
            {"server_home": "/home/example", "known_basenames": ["mail-forward-command.php"]},
            json.loads(runner.calls[2][1]),
        )

    def test_release_validation_accepts_bounded_unrelated_public_symlink_count(self):
        release_result = {
            "schema_version": 1, "manifest": "PASS", "php_cli": "PASS",
            "autoload": "PASS", "absolute_cli_dry_run": "PASS", "symlinks": 0,
        }
        audit_result = {
            "schema_version": 3, "home_symlink": False, "home_mode": 0o700,
            "public_roots_scanned": 2, "symlinks": 3, "known_product_matches": 0,
            "untrusted_subtrees": 4, "untrusted_entries": 5,
        }
        validator, _ = self.make([
            completed(self.ssh_g()), completed(json.dumps(release_result).encode()),
            completed(json.dumps(audit_result).encode()),
        ])
        result = validator.validate_release(
            [], remote_root="/home/example/private/releases/release-test",
            entrypoint="bin/app.php", server_home="/home/example",
            config_path="/home/example/mail-lineworks/private/config.json",
            expected_hosts=("remote.example.invalid",), known_basenames=[],
        )
        self.assertEqual("PASS", result["public_root"])

    def test_release_validation_rejects_inexact_or_unsafe_multi_root_audit_results(self):
        release_result = {
            "schema_version": 1, "manifest": "PASS", "php_cli": "PASS",
            "autoload": "PASS", "absolute_cli_dry_run": "PASS", "symlinks": 0,
        }
        valid_audit = {
            "schema_version": 3, "home_symlink": False, "home_mode": 0o701,
            "public_roots_scanned": 0, "symlinks": 0, "known_product_matches": 0,
            "untrusted_subtrees": 0, "untrusted_entries": 0,
        }
        invalid_audits = [
            {**valid_audit, "domain": "private.example.invalid"},
            {**valid_audit, "schema_version": 2},
            {**valid_audit, "public_roots_scanned": -1},
            {**valid_audit, "symlinks": -1},
            {**valid_audit, "symlinks": 10001},
            {**valid_audit, "symlinks": True},
            {**valid_audit, "known_product_matches": 1},
            {**valid_audit, "untrusted_subtrees": -1},
            {**valid_audit, "untrusted_subtrees": 10001},
            {**valid_audit, "untrusted_entries": True},
        ]
        manifest = [{
            "path": "bin/app.php", "type": "file", "mode": 0o700,
            "size": 1, "sha256": "0" * 64,
        }]
        for audit in invalid_audits:
            with self.subTest(audit=audit):
                validator, _ = self.make([
                    completed(self.ssh_g()), completed(json.dumps(release_result).encode()),
                    completed(json.dumps(audit).encode()),
                ])
                with self.assertRaises(RemoteValidationError):
                    validator.validate_release(
                        manifest, remote_root="/home/example/private/releases/release-test",
                        entrypoint="bin/app.php", server_home="/home/example",
                        config_path="/home/example/mail-lineworks/private/config.json",
                        expected_hosts=("remote.example.invalid",), known_basenames=[],
                    )

    def test_rejects_bad_trust_permissions_and_symlinks(self):
        for target, mode in ((self.ssh, 0o755), (self.config, 0o640), (self.known_hosts, 0o644)):
            original = stat.S_IMODE(target.stat().st_mode)
            os.chmod(target, mode)
            try:
                validator, _ = self.make([])
                with self.assertRaises(RemoteValidationError):
                    validator.validate({}, expected_hosts=("remote.example.invalid",))
            finally:
                os.chmod(target, original)
        self.config.unlink()
        self.config.symlink_to(self.known_hosts)
        validator, _ = self.make([])
        with self.assertRaises(RemoteValidationError):
            validator.validate({}, expected_hosts=("remote.example.invalid",))

    def test_dedicated_config_path_is_fixed_under_the_trusted_ssh_directory(self):
        validator, _ = self.make([])
        self.assertEqual(self.ssh / "xserver-mail-lineworks.conf", validator.config)
        self.assertNotEqual(self.ssh / "config", validator.config)

    def test_reads_config_through_verified_nonfollowing_descriptor(self):
        validator, _ = self.make([completed(self.ssh_g()), completed(json.dumps(self.result).encode())])
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unsafe path reopen")):
            validator.validate({}, expected_hosts=("remote.example.invalid",))

    def test_rejects_wrong_owner_without_disclosing_identity(self):
        validator, _ = self.make([])
        with patch("manager.remote_validator.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(RemoteValidationError, "権限"):
                validator.validate({}, expected_hosts=("remote.example.invalid",))

    def test_rejects_effective_mismatch_dangerous_values_and_oversize(self):
        bad_outputs = [
            self.ssh_g(hostname="other.invalid"), self.ssh_g(hostkeyalias="alternate"),
            self.ssh_g(proxycommand="helper"), self.ssh_g(localcommand="helper"),
            self.ssh_g(localforward="8080 target:80"),
            b"x" * (65536 + 1), b"hostname \xff\n",
        ]
        for output in bad_outputs:
            with self.subTest(output=output[:20]):
                validator, _ = self.make([completed(output)])
                with self.assertRaises(RemoteValidationError):
                    validator.validate({}, expected_hosts=("remote.example.invalid",))

    def test_rejects_nonzero_timeout_bad_json_and_secret_output(self):
        cases = [
            [completed(stderr=b"Host key verification failed", returncode=255)],
            [completed(self.ssh_g()), completed(returncode=255)],
            [completed(self.ssh_g()), completed(b"not-json")],
            [completed(self.ssh_g()), completed(
                b'{"manifest":"PASS","manifest":"PASS","php_cli":"PASS",'
                b'"absolute_cli_dry_run":"PASS","symlinks":[],"public_root":"PASS"}')],
            [completed(self.ssh_g()), completed(json.dumps({**self.result, "debug": "secret"}).encode())],
            [completed(self.ssh_g()), completed(json.dumps({**self.result, "php_cli": "FAIL"}).encode())],
        ]
        for responses in cases:
            with self.subTest(count=len(responses)):
                validator, _ = self.make(responses)
                with self.assertRaises(RemoteValidationError):
                    validator.validate({}, expected_hosts=("remote.example.invalid",))

        def timeout(*_):
            raise TimeoutError

        validator, _ = self.make([completed(self.ssh_g())], timeout)
        with self.assertRaisesRegex(RemoteValidationError, "時間内"):
            validator.validate({}, expected_hosts=("remote.example.invalid",))

    def test_rechecks_trust_identity_immediately_before_actual_spawn(self):
        def replace_known_hosts(call_number, argv):
            if call_number == 1:
                replacement = self.ssh / "replacement"
                replacement.write_bytes(self.known_hosts.read_bytes())
                os.chmod(replacement, 0o600)
                os.replace(replacement, self.known_hosts)

        validator, runner = self.make([completed(self.ssh_g())], replace_known_hosts)
        with self.assertRaises(RemoteValidationError):
            validator.validate({}, expected_hosts=("remote.example.invalid",))
        self.assertEqual(1, len(runner.calls))

    def test_real_ssh_g_accepts_production_argument_shape_without_network(self):
        if not Path("/usr/bin/ssh").exists():
            self.skipTest("OpenSSH not installed")
        validator, _ = self.make([])
        argv = validator.inspection_argv()
        completed_process = subprocess.run(argv, input=None, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, timeout=5, check=False)
        self.assertEqual(0, completed_process.returncode, "ssh -G rejected production argv shape")

        # Real macOS OpenSSH commonly emits repeatable identityfile/sendenv keys.
        # Parsing this production output is a regression check, not merely argv syntax validation.
        resolved_host = next(
            line.split(b" ", 1)[1].strip().decode("utf-8")
            for line in completed_process.stdout.splitlines() if line.startswith(b"hostname ")
        )
        production_home_validator = RemoteValidator("safe-alias", None, home=Path.home())
        production_home_validator._parse_g(completed_process.stdout, (resolved_host,))

    def test_real_ssh_g_does_not_read_the_default_user_config(self):
        if not Path("/usr/bin/ssh").exists():
            self.skipTest("OpenSSH not installed")
        default_config = self.ssh / "config"
        default_config.write_text(
            "Include unsafe-fragment\nHost safe-alias\n HostName wrong.example.invalid\n",
            encoding="utf-8",
        )
        os.chmod(default_config, 0o600)

        completed_process = subprocess.run(
            RemoteValidator("safe-alias", home=self.home).inspection_argv(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
        )

        self.assertEqual(0, completed_process.returncode)
        self.assertIn(b"hostname remote.example.invalid\n", completed_process.stdout)
        self.assertNotIn(b"wrong.example.invalid", completed_process.stdout)

    def test_production_runner_preserves_exact_bytes_and_bounds_output_and_time(self):
        result = bounded_subprocess_run(
            ["/bin/sh", "-c", "IFS= read -r value; printf '%s' \"$value\"; printf err >&2"],
            input=b"exact-bytes\n", timeout=2, stdout_limit=32, stderr_limit=32,
        )
        self.assertEqual(b"exact-bytes", result.stdout)
        self.assertEqual(b"err", result.stderr)
        with self.assertRaises(RemoteValidationError):
            bounded_subprocess_run(["/usr/bin/yes"], timeout=2, stdout_limit=8, stderr_limit=8)
        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_subprocess_run(["/bin/sleep", "2"], timeout=0, stdout_limit=8, stderr_limit=8)

    def test_real_system_php_fixed_runtime_contract_on_temporary_tree(self):
        php = "/usr/bin/php" if Path("/usr/bin/php").exists() else "php"
        with tempfile.TemporaryDirectory() as temporary:
            trusted = Path(temporary) / "trusted"
            trusted.mkdir(mode=0o755)
            account = trusted / "account"
            account.mkdir(mode=0o700)
            root = account / "fixed"
            code = fixed_runtime_php(str(trusted), os.getuid())
            def run(entries, path=root):
                return subprocess.run(
                    [php, "-r", code],
                    input=json.dumps({"root": str(path), "entries": entries},
                                     sort_keys=True, separators=(",", ":")).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                )
            self.assertEqual(
                {"state": "ABSENT", "present_files": []}, json.loads(run({}).stdout))
            root.mkdir(mode=0o700)
            (root / "dir").mkdir(mode=0o700)
            partial = {"dir": {"type": "directory", "mode": 0o700,
                               "size": 0, "sha256": None}}
            self.assertEqual("EXACT", json.loads(run(partial).stdout)["state"])
            expected = dict(partial)
            expected["dir/file.php"] = {"type": "file", "mode": 0o600,
                                         "size": 4, "sha256": hashlib.sha256(b"data").hexdigest()}
            self.assertEqual(
                {"state": "PARTIAL", "present_files": []},
                json.loads(run(expected).stdout),
            )
            target = root / "dir/file.php"
            target.write_bytes(b"data"); target.chmod(0o600)
            self.assertEqual(
                {"state": "EXACT", "present_files": ["dir/file.php"]},
                json.loads(run(expected).stdout),
            )
            target.write_bytes(b"bad!")
            failed = run(expected)
            self.assertNotEqual(0, failed.returncode)
            self.assertNotIn(str(root).encode(), failed.stdout + failed.stderr)
            target.write_bytes(b"data")
            (root / "extra").write_bytes(b"x"); (root / "extra").chmod(0o600)
            self.assertNotEqual(0, run(expected).returncode)
            self.assertNotEqual(0, run({}, trusted / "public_html" / "fixed").returncode)

    def test_real_system_php_fixed_helper_is_idempotent_and_rejects_mismatch_and_paths(self):
        self.assertTrue(hasattr(remote_validator_module, "fixed_helper_php"))
        php = "/usr/bin/php" if Path("/usr/bin/php").exists() else "php"
        with tempfile.TemporaryDirectory() as temporary:
            trusted = Path(temporary) / "home"
            trusted.mkdir(mode=0o755)
            account = trusted / "account"
            account.mkdir(mode=0o700)
            private = account / "private"
            private.mkdir(mode=0o700)
            root = private / "xserver-mail-lineworks"
            root.mkdir(mode=0o700)
            bootstrap = root / "bootstrap"
            bootstrap.mkdir(mode=0o700)
            body = b"<?php exact helper;\n"
            digest = hashlib.sha256(body).hexdigest()
            code = remote_validator_module.fixed_helper_php(str(trusted), os.getuid())

            def invoke(*, request_root=root,
                       relative="bootstrap/manage-private-config.php", value=body,
                       expected=digest, mode=0o700):
                payload = {
                    "schema_version": 1, "root": str(request_root),
                    "relative": relative,
                    "body_base64": base64.b64encode(value).decode("ascii"),
                    "sha256": expected, "mode": mode,
                }
                return subprocess.run(
                    [php, "-r", code], input=json.dumps(payload).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                )

            changed = invoke()
            self.assertEqual(0, changed.returncode, changed.stderr)
            self.assertEqual({"status": "changed"}, json.loads(changed.stdout))
            target = bootstrap / "manage-private-config.php"
            self.assertEqual(body, target.read_bytes())
            self.assertEqual(0o700, stat.S_IMODE(target.stat().st_mode))
            unchanged = invoke()
            self.assertEqual(0, unchanged.returncode, unchanged.stderr)
            self.assertEqual({"status": "unchanged"}, json.loads(unchanged.stdout))

            target.write_bytes(b"nonexact helper")
            target.chmod(0o700)
            mismatch = invoke()
            self.assertNotEqual(0, mismatch.returncode)
            self.assertEqual(b"nonexact helper", target.read_bytes())
            target.unlink()
            outside = Path(temporary) / "outside-helper"
            outside.write_bytes(b"outside")
            outside.chmod(0o700)
            target.symlink_to(outside)
            self.assertNotEqual(0, invoke().returncode)
            self.assertEqual(b"outside", outside.read_bytes())
            target.unlink()
            for request_root, relative in (
                (trusted / "account/public_html/xserver-mail-lineworks",
                 "bootstrap/manage-private-config.php"),
                (root, "bootstrap/other.php"),
                (root / "..", "bootstrap/manage-private-config.php"),
                (Path(str(trusted) + "/../private/xserver-mail-lineworks"),
                 "bootstrap/manage-private-config.php"),
            ):
                with self.subTest(root=request_root, relative=relative):
                    self.assertNotEqual(
                        0, invoke(request_root=request_root, relative=relative).returncode)

    def test_real_system_php_fixed_runtime_ignores_only_safe_mutable_top_level_trees(self):
        php = "/usr/bin/php" if Path("/usr/bin/php").exists() else "php"
        with tempfile.TemporaryDirectory() as temporary:
            trusted = Path(temporary) / "trusted"
            trusted.mkdir(mode=0o755)
            account = trusted / "account"
            account.mkdir(mode=0o700)
            root = account / "fixed"
            root.mkdir(mode=0o700)
            bootstrap = root / "bootstrap"
            bootstrap.mkdir(mode=0o700)
            fixed = bootstrap / "validate-release.php"
            fixed.write_bytes(b"fixed")
            fixed.chmod(0o700)
            expected = {
                "bootstrap": {"type": "directory", "mode": 0o700,
                              "size": 0, "sha256": None},
                "bootstrap/validate-release.php": {
                    "type": "file", "mode": 0o700, "size": 5,
                    "sha256": hashlib.sha256(b"fixed").hexdigest(),
                },
            }
            code = fixed_runtime_php(str(trusted), os.getuid())

            def invoke(payload=None):
                return subprocess.run(
                    [php, "-r", code],
                    input=json.dumps(payload or {"root": str(root), "entries": expected},
                                     sort_keys=True, separators=(",", ":")).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                )

            for name in ("releases", "state", "deploy-transactions", "logs"):
                mutable = root / name
                mutable.mkdir(mode=0o700)
                (mutable / "arbitrary").symlink_to(fixed)
            accepted = invoke()
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(
                {"state": "EXACT",
                 "present_files": ["bootstrap/validate-release.php"]},
                json.loads(accepted.stdout),
            )

            unknown = root / "caller-selected"
            unknown.mkdir(mode=0o700)
            rejected = invoke({"root": str(root), "entries": expected,
                               "ignored_top_level": ["caller-selected"]})
            self.assertNotEqual(0, rejected.returncode,
                                "the caller must not extend the hardcoded allowlist")

    def test_real_system_php_fixed_runtime_rejects_unsafe_mutable_roots(self):
        php = "/usr/bin/php" if Path("/usr/bin/php").exists() else "php"
        with tempfile.TemporaryDirectory() as temporary:
            trusted = Path(temporary) / "trusted"
            trusted.mkdir(mode=0o755)
            account = trusted / "account"
            account.mkdir(mode=0o700)
            root = account / "fixed"
            root.mkdir(mode=0o700)
            code = fixed_runtime_php(str(trusted), os.getuid())

            def invoke():
                return subprocess.run(
                    [php, "-r", code],
                    input=json.dumps({"root": str(root), "entries": {}}).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                )

            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o755)
            self.assertNotEqual(0, invoke().returncode)
            releases.chmod(0o700)
            releases.rmdir()
            releases.symlink_to(account)
            self.assertNotEqual(0, invoke().returncode)
            releases.unlink()
            releases.write_bytes(b"not a directory")
            releases.chmod(0o600)
            self.assertNotEqual(0, invoke().returncode)

    def test_real_system_php_accepts_xserver_0701_account_home_boundary(self):
        php = "/usr/bin/php" if Path("/usr/bin/php").exists() else "php"
        with tempfile.TemporaryDirectory() as temporary:
            trusted = Path(temporary) / "home"
            trusted.mkdir(mode=0o755)
            account_home = trusted / "account"
            account_home.mkdir(mode=0o700)
            account_home.chmod(0o701)
            private = account_home / "private"
            private.mkdir(mode=0o700)
            root = private / "xserver-mail-lineworks"
            code = fixed_runtime_php(str(trusted), os.getuid())

            completed = subprocess.run(
                [php, "-r", code],
                input=json.dumps({"root": str(root), "entries": {}}).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                {"state": "ABSENT", "present_files": []},
                json.loads(completed.stdout),
            )

    def test_real_system_php_rejects_unsafe_account_home_and_loose_private_suffix_modes(self):
        php = "/usr/bin/php" if Path("/usr/bin/php").exists() else "php"
        with tempfile.TemporaryDirectory() as temporary:
            trusted = Path(temporary) / "home"
            trusted.mkdir(mode=0o755)
            account_home = trusted / "account"
            account_home.mkdir(mode=0o700)
            private = account_home / "private"
            private.mkdir(mode=0o700)
            root = private / "xserver-mail-lineworks"
            code = fixed_runtime_php(str(trusted), os.getuid())

            def invoke():
                return subprocess.run(
                    [php, "-r", code],
                    input=json.dumps({"root": str(root), "entries": {}}).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                )

            for unsafe_home_mode in (0o702, 0o711, 0o771):
                account_home.chmod(unsafe_home_mode)
                self.assertNotEqual(
                    0, invoke().returncode,
                    f"account home mode {unsafe_home_mode:o} must be rejected",
                )

            account_home.chmod(0o701)
            private.chmod(0o701)
            self.assertNotEqual(0, invoke().returncode,
                                "0701 is allowed only at the account-home boundary")

    def test_real_system_php_rejects_writable_trust_and_symlinked_or_loose_suffix(self):
        php = "/usr/bin/php" if Path("/usr/bin/php").exists() else "php"
        with tempfile.TemporaryDirectory() as temporary:
            trusted = Path(temporary) / "trusted"; trusted.mkdir(mode=0o777)
            trusted.chmod(0o777)
            account = trusted / "account"; account.mkdir(mode=0o700)
            code = fixed_runtime_php(str(trusted), os.getuid())
            def invoke(path):
                return subprocess.run([php, "-r", code], input=json.dumps({"root": str(path), "entries": {}}).encode(),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertNotEqual(0, invoke(account / "fixed").returncode)
            trusted.chmod(0o755); account.chmod(0o755)
            self.assertNotEqual(0, invoke(account / "fixed").returncode)
            account.rmdir(); account.symlink_to(Path(temporary))
            self.assertNotEqual(0, invoke(account / "fixed").returncode)
            wrong_uid_code = fixed_runtime_php(str(trusted), os.getuid() + 1)
            wrong = subprocess.run([php, "-r", wrong_uid_code], input=json.dumps({"root": str(account / "fixed"), "entries": {}}).encode(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertNotEqual(0, wrong.returncode)

    @unittest.skipUnless(os.environ.get("XSERVER_RUN_SSH_CONTRACT") == "1",
                         "実Mac SSH contractは明示opt-inと別資格情報fixtureが必要")
    def test_actual_mac_contract_with_temporary_known_hosts_copies(self):
        alias = os.environ.get("XSERVER_SSH_ALIAS", "")
        self.assertTrue(alias, "XSERVER_SSH_ALIAS is required for the opt-in contract")
        validator = RemoteValidator(alias, None, home=Path.home())
        inspect = subprocess.run(validator.inspection_argv(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=5, check=False,
                                 env={**os.environ, "LC_ALL": "C"})
        self.assertEqual(0, inspect.returncode, "SSH contract preflight failed")
        fields = {}
        for line in inspect.stdout.splitlines():
            key, _, value = line.partition(b" ")
            if key in {b"hostname", b"port"}:
                fields[key] = value.strip()
        self.assertEqual({b"hostname", b"port"}, set(fields))
        hostname = fields[b"hostname"].decode("utf-8")
        port = fields[b"port"].decode("ascii")
        lookup = hostname if port == "22" else f"[{hostname}]:{port}"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normal = root / "known_hosts.normal"
            unknown = root / "known_hosts.unknown"
            changed = root / "known_hosts.changed"
            for target in (normal, unknown, changed):
                target.write_bytes(validator.known_hosts.read_bytes())
                os.chmod(target, 0o600)

            for target in (unknown, changed):
                removed = subprocess.run(
                    ["/usr/bin/ssh-keygen", "-R", lookup, "-f", str(target)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
                )
                self.assertEqual(0, removed.returncode, "known_hosts fixture preparation failed")

            key_path = root / "replacement"
            generated = subprocess.run(
                ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
            )
            self.assertEqual(0, generated.returncode, "replacement host-key generation failed")
            public_parts = key_path.with_suffix(".pub").read_text(encoding="ascii").split()
            with changed.open("a", encoding="ascii") as stream:
                stream.write(f"{lookup} {public_parts[0]} {public_parts[1]}\n")

            def actual(path):
                options = [item for pair in zip(("-o",) * len(validator.OPTIONS),
                                                validator.OPTIONS) for item in pair]
                replacement = f"UserKnownHostsFile={path}"
                options[options.index("UserKnownHostsFile=%d/.ssh/known_hosts")] = replacement
                argv = [validator.SSH, "-F", str(validator.config), *options,
                        "--", alias, "/usr/bin/true"]
                return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      timeout=30, check=False,
                                      env={**os.environ, "LC_ALL": "C"})

            normal_result = actual(normal)
            unknown_result = actual(unknown)
            changed_result = actual(changed)
            self.assertEqual(0, normal_result.returncode, "normal SSH contract failed")
            self.assertNotEqual(0, unknown_result.returncode, "unknown host key was accepted")
            self.assertNotEqual(0, changed_result.returncode, "changed host key was accepted")
            self.assertIn(b"No ED25519 host key is known", unknown_result.stderr)
            self.assertIn(b"REMOTE HOST IDENTIFICATION HAS CHANGED", changed_result.stderr)


if __name__ == "__main__":
    unittest.main()
