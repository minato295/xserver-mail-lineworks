import io
import json
import os
import ssl
import tempfile
import unittest
from ftplib import error_perm, error_proto, error_temp
from pathlib import Path
from unittest.mock import patch

from manager.ftps_deployer import FtpsDeployer, _build_verified_ssl_context, _validate_ca_file
from manager.keychain import Keychain


class FakeFtps:
    def __init__(self, files=None, mlst_response=None, modes=None,
                 retr_error=None, mlst_error=None):
        self.calls = []
        self.files = dict(files or {})
        self.mlst_response = mlst_response
        self.modes = dict(modes or {})
        self.retr_error = retr_error
        self.mlst_error = mlst_error

    def connect(self, host, port, timeout=None): self.calls.append(("connect", host, port, timeout))
    def login(self, user, password): self.calls.append(("login", user, password))
    def set_pasv(self, enabled): self.calls.append(("pasv", enabled))
    def prot_p(self): self.calls.append(("prot_p",))
    def mkd(self, path): self.calls.append(("mkd", path))
    def storbinary(self, command, stream):
        body = stream.read()
        self.calls.append(("store", command, body))
        self.files[command.removeprefix("STOR ")] = body
    def retrbinary(self, command, callback):
        self.calls.append(("retrieve", command))
        if self.retr_error is not None:
            raise self.retr_error
        path = command.removeprefix("RETR ")
        if path not in self.files:
            raise error_perm("550 No such file")
        callback(self.files[path])
    def sendcmd(self, command):
        self.calls.append(("sendcmd", command))
        if command.startswith("MLST "):
            if self.mlst_error is not None:
                raise self.mlst_error
            path = command.removeprefix("MLST ")
            if path in self.modes:
                return "250-Listing\n unix.mode=0%s;type=file; %s\n250 End" % (
                    self.modes[path], path
                )
            return self.mlst_response
    def rename(self, source, target):
        self.calls.append(("rename", source, target))
        if source in self.files:
            self.files[target] = self.files.pop(source)
    def delete(self, path):
        self.calls.append(("delete", path))
        self.files.pop(path, None)
    def rmd(self, path): self.calls.append(("rmd", path))
    def quit(self): self.calls.append(("quit",))


class FtpsDeployerTest(unittest.TestCase):
    def make_deployer(self):
        ftps = FakeFtps()
        return FtpsDeployer("ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps), ftps

    def test_default_factory_uses_verified_cafile_and_keeps_peer_and_hostname_checks(self):
        context = object()
        deployer = FtpsDeployer("ftp.example.invalid", "user", "password")
        with patch("manager.ftps_deployer._build_verified_ssl_context", return_value=context), \
                patch("manager.ftps_deployer.FTP_TLS") as ftp_tls:
            deployer._ftp_factory()
        ftp_tls.assert_called_once_with(context=context)

    def test_built_context_requires_certificate_and_hostname_validation(self):
        trusted = _validate_ca_file("/etc/ssl/cert.pem")
        with patch("manager.ftps_deployer._resolve_ca_file", return_value=trusted):
            result = _build_verified_ssl_context()
        self.assertEqual(ssl.CERT_REQUIRED, result.verify_mode)
        self.assertTrue(result.check_hostname)
        self.assertGreaterEqual(result.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertGreater(result.cert_store_stats()["x509_ca"], 1)

    def test_ca_file_validation_accepts_only_secure_nonempty_pem_regular_file(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("manager.ftps_deployer._TRUSTED_CA_OWNER_UID", os.getuid()):
            root = Path(directory)
            valid = root / "valid.pem"
            valid.write_bytes(b"-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n")
            valid.chmod(0o644)
            self.assertEqual(valid.read_bytes(), _validate_ca_file(str(valid)))

            symlink = root / "link.pem"
            symlink.symlink_to(valid)
            writable = root / "writable.pem"
            writable.write_bytes(valid.read_bytes())
            writable.chmod(0o664)
            malformed = root / "malformed.pem"
            malformed.write_bytes(b"not a certificate")
            empty = root / "empty.pem"
            empty.write_bytes(b"")
            directory_path = root / "directory.pem"
            directory_path.mkdir()
            for path in (symlink, writable, malformed, empty, directory_path, root / "missing.pem"):
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    _validate_ca_file(str(path))

    def test_ca_file_validation_rejects_wrong_owner_and_oversized_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "ca.pem")
            path.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
            path.chmod(0o600)
            with patch("manager.ftps_deployer._TRUSTED_CA_OWNER_UID", os.getuid() + 1):
                with self.assertRaises(RuntimeError):
                    _validate_ca_file(str(path))
            with patch("manager.ftps_deployer._TRUSTED_CA_OWNER_UID", os.getuid()), \
                    patch("manager.ftps_deployer._MAX_CA_FILE_SIZE", 8):
                with self.assertRaises(RuntimeError):
                    _validate_ca_file(str(path))

    def test_ca_file_validation_rejects_identity_change_between_lstat_and_open(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("manager.ftps_deployer._TRUSTED_CA_OWNER_UID", os.getuid()):
            path = Path(directory, "ca.pem")
            path.write_bytes(b"-----BEGIN CERTIFICATE-----\n")
            path.chmod(0o600)
            actual = path.stat()
            replaced = type("OpenedStat", (), {
                "st_dev": actual.st_dev,
                "st_ino": actual.st_ino + 1,
                "st_mode": actual.st_mode,
                "st_uid": actual.st_uid,
                "st_size": actual.st_size,
            })()
            with patch("manager.ftps_deployer.os.fstat", return_value=replaced):
                with self.assertRaises(RuntimeError):
                    _validate_ca_file(str(path))

    def test_ca_resolver_prefers_secure_python_default_then_fixed_macos_fallback(self):
        from manager.ftps_deployer import _resolve_ca_file

        paths = type("Paths", (), {"cafile": "/python/default.pem"})()
        with patch("manager.ftps_deployer.ssl.get_default_verify_paths", return_value=paths), \
                patch("manager.ftps_deployer._validate_ca_file", side_effect=lambda path: path.encode()) as validate:
            self.assertEqual(b"/python/default.pem", _resolve_ca_file())
        validate.assert_called_once_with("/python/default.pem")

        with patch("manager.ftps_deployer.ssl.get_default_verify_paths", return_value=paths), \
                patch("manager.ftps_deployer._validate_ca_file", side_effect=[RuntimeError(), b"trusted pem"]) as validate:
            self.assertEqual(b"trusted pem", _resolve_ca_file())
        self.assertEqual([unittest.mock.call("/python/default.pem"), unittest.mock.call("/etc/ssl/cert.pem")], validate.call_args_list)

    def test_ca_resolver_fails_closed_when_no_verified_bundle_exists(self):
        from manager.ftps_deployer import _resolve_ca_file

        paths = type("Paths", (), {"cafile": None})()
        with patch("manager.ftps_deployer.ssl.get_default_verify_paths", return_value=paths), \
                patch("manager.ftps_deployer._validate_ca_file", side_effect=RuntimeError("private detail")):
            with self.assertRaisesRegex(RuntimeError, "CA証明書を安全に読み込めません") as raised:
                _resolve_ca_file()
        self.assertNotIn("private detail", str(raised.exception))

    def test_context_consumes_validated_bytes_without_reopening_replaced_path(self):
        trusted = _validate_ca_file("/etc/ssl/cert.pem")
        with tempfile.TemporaryDirectory() as directory:
            replaced = Path(directory, "ca.pem")
            replaced.write_bytes(b"not trusted")
            with patch("manager.ftps_deployer._resolve_ca_file", return_value=trusted), \
                    patch("manager.ftps_deployer.os.open", side_effect=AssertionError("path reopened")):
                context = _build_verified_ssl_context()
        self.assertGreater(context.cert_store_stats()["x509_ca"], 1)

    def test_context_rejects_malformed_or_truncated_pem_with_fixed_error(self):
        for body in (b"not a certificate", b"-----BEGIN CERTIFICATE-----\ntruncated"):
            with self.subTest(body=body), patch("manager.ftps_deployer._resolve_ca_file", return_value=body):
                with self.assertRaisesRegex(RuntimeError, "CA証明書を安全に読み込めません") as raised:
                    _build_verified_ssl_context()
                self.assertNotIn("truncated", str(raised.exception))

    def test_bounded_readback_uses_protected_ftps_and_rejects_oversize(self):
        path = "/home/example/private/release/file.php"
        ftps = FakeFtps({path: b"1234"})
        deployer = FtpsDeployer("ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps)
        self.assertEqual(b"1234", deployer.read_bytes(path, limit=4))
        with self.assertRaises(RuntimeError):
            deployer.read_bytes(path, limit=3)

    def test_atomic_bytes_replace_returns_download_readback(self):
        path = "/home/example/private/state/active-release.json"
        deployer, ftps = self.make_deployer()
        self.assertEqual(b"{}\n", deployer.replace_bytes_atomic(path, b"{}\n", mode="600"))
        self.assertEqual(b"{}\n", ftps.files[path])
        self.assertLess(
            next(i for i, call in enumerate(ftps.calls) if call[0] == "rename"),
            next(i for i, call in enumerate(ftps.calls) if call[0] == "retrieve"),
        )

    def test_exact_remote_mode_requires_one_mlst_owner_only_fact(self):
        path = "/home/example/private/file.php"
        ftps = FakeFtps(mlst_response="250-Listing\n unix.mode=0600;type=file; file.php\n250 End")
        deployer = FtpsDeployer("ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps)
        deployer.assert_file_mode(path, "600")
        for response in ("250 type=file; file.php", "250 unix.mode=0640;type=file; file.php"):
            ftps.mlst_response = response
            with self.assertRaises(RuntimeError):
                deployer.assert_file_mode(path, "600")

    def test_batch_verification_uses_one_protected_connection_for_many_files(self):
        first = "/private/runtime/bootstrap.php"
        second = "/private/runtime/vendor/autoload.php"
        ftps = FakeFtps(
            {first: b"bootstrap", second: b"autoload"},
            modes={first: "700", second: "600"},
        )
        created = []

        def factory():
            created.append(ftps)
            return ftps

        deployer = FtpsDeployer("ftp.example.invalid", "user", "password", ftp_factory=factory)
        self.assertTrue(deployer.verify_private_files({
            first: (b"bootstrap", "700"),
            second: (b"autoload", "600"),
        }, allow_all_missing=True))
        self.assertEqual(1, len(created))
        self.assertEqual(1, sum(call[0] == "connect" for call in ftps.calls))
        self.assertEqual(1, sum(call[0] == "login" for call in ftps.calls))
        self.assertEqual(1, sum(call[0] == "prot_p" for call in ftps.calls))
        self.assertEqual(1, sum(call[0] == "quit" for call in ftps.calls))

    def test_hash_verification_binds_size_digest_mode_on_one_protected_connection(self):
        path = "/private/runtime/bootstrap.php"
        body = b"bootstrap-placeholder"
        ftps = FakeFtps({path: body}, modes={path: "700"})
        deployer = FtpsDeployer(
            "ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps)
        expected = {path: {"size": len(body),
                           "sha256": __import__("hashlib").sha256(body).hexdigest(),
                           "mode": "700"}}
        self.assertTrue(deployer.verify_private_file_hashes(expected))
        self.assertLess(ftps.calls.index(("prot_p",)),
                        ftps.calls.index(("retrieve", "RETR " + path)))
        for changed in (
            {**expected[path], "size": len(body) + 1},
            {**expected[path], "sha256": "0" * 64},
            {**expected[path], "mode": "600"},
        ):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                deployer.verify_private_file_hashes({path: changed})

    def test_hash_subset_verification_returns_only_exact_present_allowlisted_files(self):
        first = "/private/.fixed-backup-staging-example/bootstrap/a.php"
        second = "/private/.fixed-backup-staging-example/bootstrap/b.php"
        first_body = b"reviewed-a"
        second_body = b"reviewed-b"
        expected = {
            first: {"size": len(first_body), "sha256": __import__("hashlib").sha256(
                first_body).hexdigest(), "mode": "700"},
            second: {"size": len(second_body), "sha256": __import__("hashlib").sha256(
                second_body).hexdigest(), "mode": "700"},
        }
        for files, modes, present in (
                ({first: first_body}, {first: "700"}, frozenset({first})),
                ({}, {}, frozenset()),
        ):
            with self.subTest(files=tuple(files)):
                ftps = FakeFtps(files, modes=modes)
                deployer = FtpsDeployer(
                    "ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps)
                self.assertEqual(
                    present,
                    deployer.verify_private_file_hash_subset(expected, present),
                )
        for files, modes in (
                ({first: b"tampered"}, {first: "700"}),
                ({first: first_body}, {first: "600"}),
        ):
            with self.subTest(rejected=tuple(files)), self.assertRaises(RuntimeError):
                FtpsDeployer(
                    "ftp.example.invalid", "user", "password",
                    ftp_factory=lambda: FakeFtps(files, modes=modes),
                ).verify_private_file_hash_subset(expected, {first})

        for invalid in ({"/private/unknown.php"}, {first, "/private/unknown.php"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                FtpsDeployer(
                    "ftp.example.invalid", "user", "password",
                    ftp_factory=lambda: FakeFtps(),
                ).verify_private_file_hash_subset(expected, invalid)

    def test_hash_subset_verification_normalizes_retr_and_mlst_failures(self):
        path = "/private/.fixed-backup-staging-example/bootstrap/a.php"
        body = b"reviewed-a"
        expected = {path: {
            "size": len(body),
            "sha256": __import__("hashlib").sha256(body).hexdigest(),
            "mode": "700",
        }}
        failures = (
            {"retr_error": error_perm("550 Permission denied")},
            {"retr_error": error_perm("550 No such file")},
            {"retr_error": error_proto("500 malformed RETR reply")},
            {"mlst_error": error_perm("550 Permission denied")},
            {"mlst_error": error_temp("451 MLST unavailable")},
            {"mlst_error": error_proto("500 malformed MLST reply")},
            {"mlst_response": "250 malformed MLST response"},
        )
        for failure in failures:
            with self.subTest(failure=failure), self.assertRaisesRegex(
                    RuntimeError, "^remote file could not be verified$"):
                modes = {} if "mlst_response" in failure else {path: "700"}
                ftps = FakeFtps({path: body}, modes=modes, **failure)
                FtpsDeployer(
                    "ftp.example.invalid", "user", "password",
                    ftp_factory=lambda: ftps,
                ).verify_private_file_hash_subset(expected, {path})

    def test_batch_verification_returns_false_only_when_every_path_is_missing(self):
        paths = {
            "/private/runtime/a.php": (b"a", "600"),
            "/private/runtime/b.php": (b"b", "700"),
        }
        ftps = FakeFtps()
        deployer = FtpsDeployer("ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps)
        self.assertFalse(deployer.verify_private_files(paths, allow_all_missing=True))
        with self.assertRaises(RuntimeError):
            deployer.verify_private_files(paths)

    def test_batch_verification_rejects_oversize_partial_content_and_mode_failures(self):
        first = "/private/runtime/a.php"
        second = "/private/runtime/b.php"
        expected = {first: (b"a", "600"), second: (b"b", "700")}
        cases = (
            ({first: b"aa", second: b"b"}, {first: "600", second: "700"}),
            ({first: b"a"}, {first: "600"}),
            ({first: b"x", second: b"b"}, {first: "600", second: "700"}),
            ({first: b"a", second: b"b"}, {first: "640", second: "700"}),
        )
        for files, modes in cases:
            with self.subTest(files=files, modes=modes):
                ftps = FakeFtps(files, modes=modes)
                deployer = FtpsDeployer(
                    "ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps
                )
                with self.assertRaises(RuntimeError):
                    deployer.verify_private_files(expected, allow_all_missing=True)

    def test_batch_verification_rejects_ambiguous_mlst_and_invalid_inputs(self):
        path = "/private/runtime/a.php"
        for response in (
            "250 type=file; a.php",
            "250 unix.mode=0600;unix.mode=0600;type=file; a.php",
            "250 unix.mode=600;type=file; a.php",
            "250 unix.mode=0600;type=dir; a.php",
        ):
            with self.subTest(response=response):
                ftps = FakeFtps({path: b"a"}, mlst_response=response)
                deployer = FtpsDeployer(
                    "ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps
                )
                with self.assertRaises(RuntimeError):
                    deployer.verify_private_files({path: (b"a", "600")})
        deployer, _ = self.make_deployer()
        for expected in ({}, {"relative": (b"a", "600")}, {path: ("a", "600")},
                         {path: (b"a", "644")}):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                deployer.verify_private_files(expected)

    def test_batch_verification_accepts_701_only_for_fixed_or_generation_wrapper(self):
        permanent = "/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php"
        staging = "/private/.fixed-staging-0123456789abcdef0123456789abcdef/bootstrap/mail-forward-command-701.php"
        invalid_paths = (
            "/private/runtime/mail-forward-command-701.php",
            "/private/xserver-mail-lineworks/bootstrap/other.php",
            "/private/.fixed-staging-ABCDEF0123456789abcdef0123456789/bootstrap/mail-forward-command-701.php",
            "/private/.fixed-staging-0123/bootstrap/mail-forward-command-701.php",
        )
        all_paths = (permanent, staging, *invalid_paths)
        ftps = FakeFtps({path: b"<?php" for path in all_paths},
                        modes={path: "701" for path in all_paths})
        deployer = FtpsDeployer(
            "ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps
        )
        for path in (permanent, staging):
            self.assertTrue(deployer.verify_private_files({path: (b"<?php", "701")}))
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                deployer.verify_private_files({path: (b"<?php", "701")})
            with self.assertRaises(ValueError):
                deployer.assert_file_mode(path, "701")
        for invalid in ("601", "711", "755"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                deployer.verify_private_files({permanent: (b"<?php", invalid)})

    def test_deploy_preserves_exact_allowed_local_file_modes(self):
        deployer, ftps = self.make_deployer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, mode in (("plain", 0o600), ("executable", 0o700),
                               ("mail-forward-command-701.php", 0o701)):
                path = root / name
                path.write_bytes(b"x")
                path.chmod(mode)
            deployer.deploy_release(root, "/private/xserver-mail-lineworks/bootstrap")
        chmod = [call[1] for call in ftps.calls if call[0] == "sendcmd" and "CHMOD" in call[1]]
        self.assertTrue(any("CHMOD 600 " in item for item in chmod))
        self.assertTrue(any("CHMOD 700 " in item for item in chmod))
        self.assertTrue(any("CHMOD 701 " in item for item in chmod))

    def test_deploy_rejects_701_outside_exact_fixed_wrapper_location(self):
        deployer, _ = self.make_deployer()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "mail-forward-command-701.php")
            path.write_bytes(b"<?php")
            path.chmod(0o701)
            with self.assertRaises(ValueError):
                deployer.deploy_release(directory, "/private/runtime")

    def test_batch_verification_binds_mlst_entry_to_exact_requested_path(self):
        path = "/private/runtime/a.php"
        responses = (
            "250 unix.mode=0600;type=file; /private/runtime/unrelated.php",
            "250 unix.mode=0600;type=file; a.php",
            "250-Listing\n unix.mode=0600;type=file; %s\n"
            " unix.mode=0600;type=file; /private/runtime/other.php\n250 End" % path,
            "250-Listing\n unix.mode=0600;type=file; %s\n"
            " unix.mode=0600;type=file; %s\n250 End" % (path, path),
        )
        for response in responses:
            with self.subTest(response=response):
                ftps = FakeFtps({path: b"a"}, mlst_response=response)
                deployer = FtpsDeployer(
                    "ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps
                )
                with self.assertRaises(RuntimeError):
                    deployer.verify_private_files({path: (b"a", "600")})

    def test_fixed_tree_publish_is_one_sibling_rename_and_cleanup_is_allowlisted(self):
        deployer, ftps = self.make_deployer()
        staging = "/private/.fixed-staging-test"
        final = "/private/xserver-mail-lineworks"
        deployer.publish_directory(staging, final)
        self.assertEqual(1, sum(call[0] == "rename" for call in ftps.calls))
        deployer.delete_exact_tree(staging, ["a/file.php"], ["a"])
        self.assertIn(("delete", staging + "/a/file.php"), ftps.calls)
        self.assertEqual(("rmd", staging), ftps.calls[-2])

    def test_deploy_rejects_public_html_remote_root(self):
        deployer, _ = self.make_deployer()
        forbidden = "public" + "_html"
        for remote_root in (
            "/home/example/" + forbidden + "/app",
            "/home/example/" + forbidden.upper() + "/app",
            "/home/example/" + forbidden.title() + "/app",
        ):
            with self.subTest(remote_root=remote_root), self.assertRaises(ValueError):
                deployer._validate_private(remote_root)

    def test_private_paths_reject_raw_dot_segments_before_normalization(self):
        deployer, _ = self.make_deployer()
        for remote_path in (
            "/home/./account/private",
            "/home/account/private/.",
        ):
            with self.subTest(remote_path=remote_path), self.assertRaises(ValueError):
                deployer._validate_private(remote_path)

    def test_private_path_validation_preserves_absolute_root_and_empty_segments(self):
        deployer, _ = self.make_deployer()

        deployer._validate_private("/home//account/private")

        with self.assertRaises(ValueError):
            deployer._validate_private("home/account/private")

    def test_private_paths_still_reject_parent_and_public_html_segments(self):
        deployer, _ = self.make_deployer()
        forbidden = "Public" + "_Html"
        for remote_path in (
            "/home/account/../private",
            "/home/account/" + forbidden + "/private",
        ):
            with self.subTest(remote_path=remote_path), self.assertRaises(ValueError):
                deployer._validate_private(remote_path)

    def test_deploy_uses_tls_private_data_temp_upload_chmod_then_rename(self):
        deployer, ftps = self.make_deployer()
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory, "app.php")
            app.write_bytes(b"<?php")
            app.chmod(0o600)
            deployer.deploy_release(directory, "/home/account/private/releases/r1")
        names = [call[0] for call in ftps.calls]
        self.assertLess(names.index("connect"), names.index("login"))
        self.assertLess(names.index("login"), names.index("prot_p"))
        self.assertIn(("pasv", True), ftps.calls)
        store = next(call for call in ftps.calls if call[0] == "store")
        self.assertIn(".tmp-", store[1])
        temporary = store[1].removeprefix("STOR ")
        chmod_index = ftps.calls.index(("sendcmd", "SITE CHMOD 600 " + temporary))
        rename_index = names.index("rename")
        self.assertLess(names.index("store"), chmod_index)
        self.assertLess(chmod_index, rename_index)
        self.assertEqual(("rename", temporary, "/home/account/private/releases/r1/app.php"), ftps.calls[rename_index])

    def test_ensure_dirs_chmods_existing_directories_to_700(self):
        class ExistingDirsFtps(FakeFtps):
            def mkd(self, path):
                super().mkd(path)
                raise error_perm("550 File exists")

        ftps = ExistingDirsFtps()

        FtpsDeployer._ensure_dirs(ftps, "/home/account/private")

        self.assertEqual([
            ("mkd", "/home"),
            ("sendcmd", "SITE CHMOD 700 /home"),
            ("mkd", "/home/account"),
            ("sendcmd", "SITE CHMOD 700 /home/account"),
            ("mkd", "/home/account/private"),
            ("sendcmd", "SITE CHMOD 700 /home/account/private"),
        ], ftps.calls)

    def test_deploy_chmods_every_nested_release_directory_to_700(self):
        deployer, ftps = self.make_deployer()
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory, "bin", "jobs")
            nested.mkdir(parents=True)
            executable = Path(nested, "notify")
            executable.write_bytes(b"#!/bin/sh\n")
            executable.chmod(0o700)

            deployer.deploy_release(directory, "/home/account/private/releases/r1")

        chmods = {
            call[1] for call in ftps.calls
            if call[0] == "sendcmd" and call[1].startswith("SITE CHMOD 700 /")
            and ".tmp-" not in call[1]
        }
        self.assertEqual({
            "SITE CHMOD 700 /home",
            "SITE CHMOD 700 /home/account",
            "SITE CHMOD 700 /home/account/private",
            "SITE CHMOD 700 /home/account/private/releases",
            "SITE CHMOD 700 /home/account/private/releases/r1",
            "SITE CHMOD 700 /home/account/private/releases/r1/bin",
            "SITE CHMOD 700 /home/account/private/releases/r1/bin/jobs",
        }, chmods)

    def test_empty_release_chmods_remote_root_and_all_parents_to_700(self):
        deployer, ftps = self.make_deployer()
        with tempfile.TemporaryDirectory() as directory:
            deployer.deploy_release(directory, "/home/account/private/releases/r1")

        self.assertEqual([
            "SITE CHMOD 700 /home",
            "SITE CHMOD 700 /home/account",
            "SITE CHMOD 700 /home/account/private",
            "SITE CHMOD 700 /home/account/private/releases",
            "SITE CHMOD 700 /home/account/private/releases/r1",
        ], [call[1] for call in ftps.calls if call[0] == "sendcmd"])

    def test_directory_chmod_failure_aborts_before_store_or_rename(self):
        class ChmodFailureFtps(FakeFtps):
            def sendcmd(self, command):
                super().sendcmd(command)
                if command.startswith("SITE CHMOD 700 "):
                    raise error_perm("550 CHMOD failed")

        ftps = ChmodFailureFtps()
        deployer = FtpsDeployer(
            "ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps,
        )
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory, "app.php")
            app.write_bytes(b"<?php")
            app.chmod(0o600)
            with self.assertRaises(error_perm):
                deployer.deploy_release(directory, "/home/account/private/releases/r1")

        self.assertFalse(any(call[0] in ("store", "rename") for call in ftps.calls))

    def test_private_config_is_uploaded_from_json_bytes(self):
        deployer, ftps = self.make_deployer()
        deployer.update_private_config({"client_id": "example", "enabled": True})
        store = next(call for call in ftps.calls if call[0] == "store")
        self.assertRegex(store[1], r"config\.json\.tmp-[0-9a-f]{32}$")
        self.assertEqual({"client_id": "example", "enabled": True}, json.loads(store[2]))
        self.assertNotIn(b"<?php", store[2])

    def test_private_config_uses_a_unique_temporary_name_for_each_upload(self):
        deployer, ftps = self.make_deployer()
        deployer.update_private_config({"token": "first"})
        deployer.update_private_config({"token": "second"})
        stores = [call[1] for call in ftps.calls if call[0] == "store"]
        self.assertEqual(2, len(stores))
        self.assertNotEqual(stores[0], stores[1])

    def test_private_config_provisions_log_parent_as_700_without_writing_log(self):
        ftps = FakeFtps()
        deployer = FtpsDeployer(
            "ftp.example.invalid", "account@example.invalid", "password",
            config_remote_path="/mail-lineworks/private/config.json",
            filesystem_home="/home/example",
            ftp_factory=lambda: ftps,
        )

        deployer.update_private_config({
            "log_path": "/home/example/mail-lineworks/private/log/mail-notifier.jsonl",
        })

        self.assertIn(("mkd", "/mail-lineworks/private/log"), ftps.calls)
        chmod = ("sendcmd", "SITE CHMOD 700 /mail-lineworks/private/log")
        self.assertIn(chmod, ftps.calls)
        config_store_index = next(i for i, call in enumerate(ftps.calls) if call[0] == "store")
        self.assertLess(ftps.calls.index(chmod), config_store_index)
        self.assertFalse(any(
            call[0] == "store" and "mail-notifier.jsonl" in call[1]
            for call in ftps.calls
        ))

    def test_private_config_rejects_log_path_outside_its_private_root(self):
        forbidden = "PUBLIC" + "_HTML"
        invalid_paths = (
            "/current/log/mail-notifier.jsonl",
            "/home/user/private",
            "/home/user/private/log/../../current/mail-notifier.jsonl",
            "/home/example/" + forbidden + "/mail-notifier.jsonl",
        )
        for log_path in invalid_paths:
            with self.subTest(log_path=log_path):
                deployer, ftps = self.make_deployer()
                with self.assertRaises(ValueError):
                    deployer.update_private_config({"log_path": log_path})
                self.assertEqual([], ftps.calls)

    def test_rejects_unsafe_filesystem_home_before_ftps_connection(self):
        forbidden = "Public" + "_Html"
        for filesystem_home in (7, "home/example", "/home/./example", "/home/example/..", "/home/example/" + forbidden):
            with self.subTest(filesystem_home=filesystem_home), self.assertRaises(ValueError):
                FtpsDeployer(
                    "ftp.example.invalid", "account@example.invalid", "password",
                    filesystem_home=filesystem_home,
                )

    def test_reads_complete_private_config_over_protected_ftps(self):
        ftps = FakeFtps({"/private/config.json": b'{"webhook_url":"redacted","error_recipients":["ops@example.invalid"],"unknown":7}'})
        deployer = FtpsDeployer("ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps)
        config = deployer.read_private_config()
        self.assertEqual(7, config["unknown"])
        self.assertEqual(["ops@example.invalid"], config["error_recipients"])
        self.assertIn(("retrieve", "RETR /private/config.json"), ftps.calls)
        self.assertLess(ftps.calls.index(("prot_p",)), ftps.calls.index(("retrieve", "RETR /private/config.json")))

    def test_invalid_private_config_is_rejected(self):
        for body in (b"not-json", b"[]"):
            with self.subTest(body=body):
                ftps = FakeFtps({"/private/config.json": body})
                deployer = FtpsDeployer("ftp.example.invalid", "user", "password", ftp_factory=lambda: ftps)
                with self.assertRaises(RuntimeError):
                    deployer.read_private_config()


class KeychainTest(unittest.TestCase):
    @patch("manager.keychain.subprocess.run")
    def test_explicit_temporary_keychain_path_is_validated_and_appended_once(self, run):
        run.return_value = type("Result", (), {"stdout": "api-secret\n", "returncode": 0})()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "test.keychain-db")
            path.write_bytes(b"test keychain container")
            path.chmod(0o600)
            keychain = Keychain(keychain_path=str(path))
            self.assertEqual("api-secret", keychain.read_api_key())
        arguments = run.call_args.args[0]
        self.assertEqual(str(path), arguments[-1])
        self.assertEqual(1, arguments.count(str(path)))

    def test_explicit_temporary_keychain_rejects_unsafe_paths_before_security(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "test.keychain-db"
            valid.write_bytes(b"test")
            valid.chmod(0o600)
            invalid = root / "not-a-keychain.txt"
            invalid.write_bytes(b"test")
            invalid.chmod(0o600)
            symlink = root / "link.keychain-db"
            symlink.symlink_to(valid)
            loose = root / "loose.keychain-db"
            loose.write_bytes(b"test")
            loose.chmod(0o640)
            for path in ("relative.keychain-db", str(invalid), str(symlink), str(loose)):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    Keychain(keychain_path=path)

    @patch("manager.keychain.subprocess.run")
    def test_reads_api_and_ftps_internet_password_schema_without_printing_secrets(self, run):
        run.side_effect = [
            type("Result", (), {"stdout": "api-secret\n", "returncode": 0})(),
            type("Result", (), {"stdout": 'attributes:\n    "acct"<blob>="ftp-user"\n', "returncode": 0})(),
            type("Result", (), {"stdout": "ftp-secret\n", "returncode": 0})(),
        ]
        keychain = Keychain()
        with patch("builtins.print") as printer:
            self.assertEqual("api-secret", keychain.read_api_key())
            self.assertEqual(("ftp-user", "ftp-secret"), keychain.read_ftps_credentials())
        printer.assert_not_called()
        self.assertEqual(
            ["security", "find-internet-password", "-s", "api.xserver.ne.jp", "-a", "Bearer",
             "-r", "htps", "-P", "443", "-w"],
            run.call_args_list[0].args[0],
        )
        self.assertEqual(
            ["security", "find-internet-password", "-s", "ftps.xserver.ne.jp",
             "-r", "ftps", "-P", "21"],
            run.call_args_list[1].args[0],
        )
        self.assertEqual(
            ["security", "find-internet-password", "-s", "ftps.xserver.ne.jp", "-a", "ftp-user",
             "-r", "ftps", "-P", "21", "-w"],
            run.call_args_list[2].args[0],
        )
        for call in run.call_args_list:
            self.assertTrue(call.kwargs["capture_output"])
            self.assertTrue(call.kwargs["text"])
            self.assertTrue(call.kwargs["check"])

    @patch("manager.keychain.subprocess.run")
    def test_rejects_missing_duplicate_or_malformed_ftps_account_without_leaking_metadata(self, run):
        private_values = ("private-account-one", "private-account-two")
        outputs = (
            "attributes:\n",
            'attributes:\n    "acct"<blob>="private-account-one"\n    "acct"<blob>="private-account-two"\n',
            'attributes:\n    "acct"<blob>=private-account-one\n',
            'attributes:\n    "acct"<blob>="private-account-one"\n    "acct"<blob>=private-account-two\n',
            'attributes:\n    "acct"<blob>="private-account-one"\n    "acct"<data>="private-account-two"\n',
        )
        for output in outputs:
            with self.subTest(output_kind=outputs.index(output)):
                run.reset_mock()
                run.return_value = type("Result", (), {"stdout": output, "returncode": 0})()
                with self.assertRaises(RuntimeError) as raised:
                    Keychain().read_ftps_credentials()
                self.assertEqual("FTPSキーチェーン項目を一意に特定できません", str(raised.exception))
                for value in private_values:
                    self.assertNotIn(value, str(raised.exception))
                self.assertEqual(1, run.call_count)

    @patch("manager.keychain.subprocess.run")
    def test_ignores_similar_metadata_labels_when_one_strict_account_exists(self, run):
        run.side_effect = [
            type("Result", (), {
                "stdout": (
                    'attributes:\n'
                    '    "acctx"<blob>="unrelated-private-value"\n'
                    '    "acct"<blob>="ftp-user"\n'
                    '    "account"<blob>="another-private-value"\n'
                ),
                "returncode": 0,
            })(),
            type("Result", (), {"stdout": "ftp-secret\n", "returncode": 0})(),
        ]

        self.assertEqual(("ftp-user", "ftp-secret"), Keychain().read_ftps_credentials())

    @patch("manager.keychain.subprocess.run")
    def test_wraps_security_failures_without_including_captured_secret_output(self, run):
        import subprocess

        run.side_effect = subprocess.CalledProcessError(
            44,
            ["security", "find-internet-password"],
            output="private-secret-output",
            stderr="private-secret-error",
        )
        with self.assertRaises(RuntimeError) as raised:
            Keychain().read_api_key()
        rendered = str(raised.exception)
        self.assertEqual("macOSキーチェーンを読み取れません", rendered)
        self.assertNotIn("private-secret", rendered)


if __name__ == "__main__":
    unittest.main()
