import json
import base64
import hashlib
import subprocess
import unittest
from unittest.mock import patch

from manager.private_config_ssh import ConfigCasResult, PrivateConfigSsh


SECRET = "secret-token-placeholder"


class FakeTrustedValidator:
    instances = []

    def __init__(self, ssh_alias, runner):
        self.ssh_alias = ssh_alias
        self.runner = runner
        self.calls = []
        self.__class__.instances.append(self)

    def run_trusted(self, remote_command, input_data, *, expected_hosts, output_limit):
        self.calls.append((remote_command, input_data, expected_hosts, output_limit))
        request = json.loads(input_data)
        if request["operation"] == "read":
            return json.dumps({
                "schema_version": 1,
                "config": {"webhook_url": SECRET, "other": "kept"},
                "sha256": "b" * 64,
            }).encode()
        return json.dumps({
            "schema_version": 1, "status": "changed",
            "old_sha256": "a" * 64, "new_sha256": "c" * 64,
        }).encode()


class PrivateConfigSshTest(unittest.TestCase):
    def setUp(self):
        FakeTrustedValidator.instances.clear()

    def make(self, **kwargs):
        options = {"expected_hosts": ["host.example.invalid"], "runner": object()}
        options.update(kwargs)
        with patch("manager.private_config_ssh.RemoteValidator", FakeTrustedValidator):
            client = PrivateConfigSsh("safe-alias", "/home/example", **options)
        return client, FakeTrustedValidator.instances[-1]

    def test_rejects_shell_metacharacters_and_noncanonical_home(self):
        invalid = (
            "/home/a b", "/home/a;id", "/home/$USER", "/home/`id`",
            "/home/a/b", "/home/-bad", "/home/", "/HOME/example",
            "/home/" + "a" * 65,
        )
        for home in invalid:
            with self.subTest(home=home), self.assertRaises(ValueError):
                PrivateConfigSsh("safe-alias", home,
                                 expected_hosts=["host.example.invalid"])

    def test_rejects_missing_duplicate_or_noncanonical_expected_hosts(self):
        invalid = ([], ["same.invalid", "same.invalid"], ["UPPER.invalid"],
                   ["-bad.invalid"], ["bad..invalid"], ["bad invalid"])
        for hosts in invalid:
            with self.subTest(hosts=hosts), self.assertRaises(ValueError):
                PrivateConfigSsh("safe-alias", "/home/example", expected_hosts=hosts)

    def test_read_uses_fixed_quoted_command_and_trusted_boundary(self):
        client, trusted = self.make()
        config, digest = client.read()
        self.assertEqual(SECRET, config["webhook_url"])
        self.assertEqual("b" * 64, digest)
        self.assertEqual(1, len(trusted.calls))
        command, input_data, hosts, limit = trusted.calls[0]
        self.assertEqual(
            "/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/"
            "bootstrap/manage-private-config.php", command,
        )
        self.assertEqual({"schema_version": 1, "operation": "read"},
                         json.loads(input_data))
        self.assertEqual(["host.example.invalid"], hosts)
        self.assertEqual(131072, limit)
        self.assertNotIn(SECRET, command)

    def test_compare_and_swap_sends_secret_only_on_stdin(self):
        client, trusted = self.make()
        result = client.compare_and_swap("a" * 64, {"webhook_url": SECRET})
        self.assertEqual(ConfigCasResult("changed", "a" * 64, "c" * 64), result)
        command, input_data, _, _ = trusted.calls[0]
        self.assertNotIn(SECRET, command)
        self.assertIn(SECRET, input_data.decode())
        self.assertEqual("a" * 64, json.loads(input_data)["expected_sha256"])

    def test_health_summary_uses_fixed_operation_and_exact_redacted_schema(self):
        client, trusted = self.make()
        expected = {
            "state": "degraded", "changed_at": "2026-07-13T00:00:00Z",
            "classification": "transport_error", "next_observation_sequence": 3,
            "last_applied_sequence": 2,
        }
        def response(remote_command, input_data, *, expected_hosts, output_limit):
            trusted.calls.append((remote_command, input_data, expected_hosts, output_limit))
            return json.dumps({"schema_version": 1, **expected}).encode()
        trusted.run_trusted = response
        self.assertEqual(expected, client.health_summary())
        request = json.loads(trusted.calls[0][1]) if trusted.calls else None
        self.assertEqual({"schema_version": 1, "operation": "health-summary"}, request)

    def test_health_summary_rejects_inexact_or_unsafe_responses(self):
        valid = {
            "schema_version": 1, "state": "healthy",
            "changed_at": "2026-07-13T00:00:00Z",
            "classification": "success", "next_observation_sequence": 0,
            "last_applied_sequence": 0,
        }
        invalid = [
            {**valid, "debug": SECRET}, {**valid, "state": "unknown"},
            {**valid, "state": []}, {**valid, "classification": {}},
            {**valid, "next_observation_sequence": True},
            {**valid, "next_observation_sequence": 0, "last_applied_sequence": 1},
            {**valid, "changed_at": "not-a-time"},
            {**valid, "changed_at": None, "classification": None},
        ]
        for response in invalid:
            client, trusted = self.make()
            trusted.run_trusted = lambda *_args, value=response, **_kwargs: json.dumps(value).encode()
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, "応答") as caught:
                client.health_summary()
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)

    def test_health_summary_accepts_exact_missing_healthy_and_degraded_schemas(self):
        summaries = [
            {"state": "missing", "changed_at": None, "classification": None,
             "next_observation_sequence": 0, "last_applied_sequence": 0},
            {"state": "healthy", "changed_at": "2026-07-13T00:00:00Z",
             "classification": "success", "next_observation_sequence": 2,
             "last_applied_sequence": 2},
            {"state": "degraded", "changed_at": "2026-07-13T00:00:01Z",
             "classification": "transport_error", "next_observation_sequence": 3,
             "last_applied_sequence": 2},
        ]
        for summary in summaries:
            client, trusted = self.make()
            trusted.run_trusted = lambda *_args, value=summary, **_kwargs: json.dumps(
                {"schema_version": 1, **value}
            ).encode()
            with self.subTest(state=summary["state"]):
                self.assertEqual(summary, client.health_summary())

    def test_health_summary_allowlist_exactly_matches_runtime_and_helper(self):
        classifications = {
            "success", "invalid_payload", "invalid_parameter", "missing_parameter",
            "invalid_webhook_url", "rate_limited", "http_error", "transport_error",
            "forced_test_failure", "internal_error", "system_mail_suppressed",
            "health_state_failure", "unknown",
        }
        for classification in sorted(classifications):
            client, trusted = self.make()
            state = "healthy" if classification == "success" else "degraded"
            response = {
                "schema_version": 1, "state": state,
                "changed_at": "2026-07-13T00:00:00Z",
                "classification": classification,
                "next_observation_sequence": 1, "last_applied_sequence": 1,
            }
            trusted.run_trusted = lambda *_args, value=response, **_kwargs: json.dumps(value).encode()
            with self.subTest(classification=classification):
                self.assertEqual(classification, client.health_summary()["classification"])
        client, trusted = self.make()
        trusted.run_trusted = lambda *_args, **_kwargs: json.dumps({
            "schema_version": 1, "state": "degraded",
            "changed_at": "2026-07-13T00:00:00Z",
            "classification": "not_allowlisted",
            "next_observation_sequence": 1, "last_applied_sequence": 1,
        }).encode()
        with self.assertRaises(RuntimeError):
            client.health_summary()

    def test_scope_journal_uses_fixed_operations_and_validates_responses(self):
        body = b'{"schema_version":2}\n'
        encoded = base64.b64encode(body).decode("ascii")
        client, trusted = self.make()
        responses = iter([
            {"schema_version": 1, "state": "missing"},
            {"schema_version": 1, "state": "present", "body_base64": encoded},
            {"schema_version": 1, "sha256": hashlib.sha256(body).hexdigest()},
        ])
        def respond(remote_command, input_data, *, expected_hosts, output_limit):
            trusted.calls.append((remote_command, input_data, expected_hosts, output_limit))
            return json.dumps(next(responses)).encode()
        trusted.run_trusted = respond
        self.assertIsNone(client.read_scope_journal("target-sync"))
        self.assertEqual(body, client.read_scope_journal("target-sync"))
        self.assertEqual(body, client.write_scope_journal("filter", body))
        requests = [json.loads(call[1]) for call in trusted.calls]
        self.assertEqual("scope-journal-read", requests[0]["operation"])
        self.assertEqual("scope-journal-read", requests[1]["operation"])
        self.assertEqual("scope-journal-write", requests[2]["operation"])
        self.assertEqual("filter", requests[2]["journal"])
        self.assertNotIn("path", requests[2])

        invalid = [
            {"schema_version": 1, "state": "present", "body_base64": "***"},
            {"schema_version": 1, "state": "missing", "body_base64": encoded},
            {"schema_version": 1, "sha256": "0" * 64},
        ]
        for index, response in enumerate(invalid):
            client, trusted = self.make()
            trusted.run_trusted = lambda *_args, value=response, **_kwargs: json.dumps(value).encode()
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                (client.read_scope_journal("target-sync") if index < 2 else
                 client.write_scope_journal("filter", body))

    def test_target_sync_journal_cas_sends_exact_expected_and_desired_without_paths(self):
        expected = b'{"schema_version":1,"phase":"active"}\n'
        desired = b'{"schema_version":1,"phase":"committed"}\n'
        client, trusted = self.make()
        trusted.run_trusted = lambda remote_command, input_data, *, expected_hosts, output_limit: (
            trusted.calls.append((remote_command, input_data, expected_hosts, output_limit))
            or json.dumps({"schema_version": 1, "status": "changed"}).encode()
        )
        self.assertEqual(desired, client.compare_and_swap_scope_journal(
            "target-sync", expected, desired))
        request = json.loads(trusted.calls[0][1])
        self.assertEqual("scope-journal-compare-and-swap", request["operation"])
        self.assertEqual({
            "state": "present", "body_base64": base64.b64encode(expected).decode(),
            "sha256": hashlib.sha256(expected).hexdigest(),
        }, request["expected"])
        self.assertEqual({
            "body_base64": base64.b64encode(desired).decode(),
            "sha256": hashlib.sha256(desired).hexdigest(),
        }, request["desired"])
        self.assertNotIn("path", request)
        self.assertEqual(131072, trusted.calls[0][3])

    def test_maximum_target_sync_cas_uses_large_input_but_bounded_output(self):
        expected = b"e" * 65536
        desired = b"d" * 65536
        client, trusted = self.make()
        trusted.run_trusted = lambda remote_command, input_data, *, expected_hosts, output_limit: (
            trusted.calls.append((remote_command, input_data, expected_hosts, output_limit))
            or json.dumps({"schema_version": 1, "status": "changed"}).encode()
        )
        self.assertEqual(desired, client.compare_and_swap_scope_journal(
            "target-sync", expected, desired))
        self.assertLessEqual(len(trusted.calls[0][1]), 262144)
        self.assertGreater(len(trusted.calls[0][1]), 131072)
        self.assertEqual(131072, trusted.calls[0][3])

    def test_target_sync_journal_cas_supports_exact_missing_expected(self):
        desired = b'{"schema_version":1}\n'
        client, trusted = self.make()
        trusted.run_trusted = lambda remote_command, input_data, *, expected_hosts, output_limit: (
            trusted.calls.append((remote_command, input_data, expected_hosts, output_limit))
            or json.dumps({"schema_version": 1, "status": "already_applied"}).encode()
        )
        self.assertEqual(desired, client.compare_and_swap_scope_journal(
            "target-sync", None, desired))
        self.assertEqual({"state": "missing"}, json.loads(trusted.calls[0][1])["expected"])

    def test_target_sync_journal_cas_reconciles_all_ambiguous_responses(self):
        expected = b'{"old":1}\n'
        desired = b'{"new":1}\n'
        for label, first, readback, succeeds, message in (
            ("transport-desired", OSError("lost"), desired, True, None),
            ("malformed-desired", b'{"bad":true}', desired, True, None),
            ("conflict-desired", {"schema_version": 1, "status": "conflict"}, desired, True, None),
            ("transport-expected", OSError("lost"), expected, False, "適用されません"),
            ("conflict-third", {"schema_version": 1, "status": "conflict"}, b'{"third":1}\n', False, "不明"),
        ):
            with self.subTest(label=label):
                client, trusted = self.make()
                calls = 0
                def respond(remote_command, input_data, *, expected_hosts, output_limit):
                    nonlocal calls
                    trusted.calls.append((remote_command, input_data, expected_hosts, output_limit))
                    calls += 1
                    if calls == 1:
                        if isinstance(first, BaseException):
                            raise first
                        if isinstance(first, bytes):
                            return first
                        return json.dumps(first).encode()
                    return json.dumps({
                        "schema_version": 1, "state": "present",
                        "body_base64": base64.b64encode(readback).decode(),
                    }).encode()
                trusted.run_trusted = respond
                if succeeds:
                    self.assertEqual(desired, client.compare_and_swap_scope_journal(
                        "target-sync", expected, desired))
                else:
                    with self.assertRaisesRegex(RuntimeError, message):
                        client.compare_and_swap_scope_journal("target-sync", expected, desired)
                self.assertEqual("scope-journal-read",
                                 json.loads(trusted.calls[1][1])["operation"])

    def test_rejects_oversize_request_before_ssh(self):
        client, trusted = self.make()
        with self.assertRaisesRegex(ValueError, "設定要求"):
            client.compare_and_swap("a" * 64, {"padding": "x" * 262144})
        self.assertEqual([], trusted.calls)

    def test_rejects_inexact_read_and_cas_response_schemas(self):
        bad_read = (
            {},
            {"schema_version": 1, "config": {}, "sha256": "a" * 64, "debug": SECRET},
            {"schema_version": True, "config": {}, "sha256": "a" * 64},
            {"schema_version": 1, "config": [], "sha256": "a" * 64},
            {"schema_version": 1, "config": {}, "sha256": "A" * 64},
        )
        for response in bad_read:
            client, trusted = self.make()
            trusted.run_trusted = lambda *_args, **_kwargs: json.dumps(response).encode()
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, "応答"):
                client.read()

        client, trusted = self.make()
        trusted.run_trusted = lambda *_args, **_kwargs: json.dumps({
            "schema_version": 1, "status": "invalid", "old_sha256": "a" * 64,
            "new_sha256": "b" * 64,
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "応答"):
            client.compare_and_swap("a" * 64, {})

    def test_rejects_duplicate_json_keys_and_non_utf8_without_leaking_secret(self):
        responses = [
            b'{"schema_version":1,"schema_version":1,"config":{},"sha256":"' + b"a" * 64 + b'"}',
            b"\xff" + SECRET.encode(),
        ]
        for response in responses:
            client, trusted = self.make()
            trusted.run_trusted = lambda *_args, value=response, **_kwargs: value
            with self.subTest(response=response), self.assertRaises(RuntimeError) as caught:
                client.read()
            pending = [caught.exception]
            seen = set()
            while pending:
                error = pending.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                self.assertNotIn(SECRET, str(error))
                self.assertNotIn(SECRET, repr(error))
                self.assertNotIn(SECRET, repr(error.args))
                pending.extend(item for item in (error.__cause__, error.__context__)
                               if item is not None)

    def test_malformed_response_types_are_redacted_without_exception_chain(self):
        for response in (None, 7, object()):
            client, trusted = self.make()
            trusted.run_trusted = lambda *_args, value=response, **_kwargs: value
            with self.subTest(response=type(response).__name__), self.assertRaises(RuntimeError) as caught:
                client.read()
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn(SECRET, repr(caught.exception))


if __name__ == "__main__":
    unittest.main()
