import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from manager.xserver_api import (
    XServerApi,
    XServerApiError,
    XServerPermissionError,
    XServerRateLimitError,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


def http_error(status, body, headers=None):
    return HTTPError(
        "https://api.xserver.ne.jp/", status, "failure", headers or {},
        io.BytesIO(json.dumps(body).encode()),
    )


class XServerApiTest(unittest.TestCase):
    def make_api(self, responses):
        transport = FakeTransport(responses)
        api = XServerApi(
            "server.example.invalid", "secret-api-key",
            "/home/example/private/mail-forward-command", transport=transport,
        )
        return api, transport

    def test_list_uses_https_bearer_and_optional_domain(self):
        api, transport = self.make_api([{"filters": []}])
        self.assertEqual([], api.list_filters("example.invalid"))
        request, timeout = transport.requests[0]
        self.assertEqual(
            "https://api.xserver.ne.jp/v1/server/server.example.invalid/mail-filter?domain=example.invalid",
            request.full_url,
        )
        self.assertEqual("Bearer secret-api-key", request.get_header("Authorization"))
        self.assertEqual("GET", request.get_method())
        self.assertIsNotNone(timeout)

    def test_list_mail_accounts_uses_documented_collection_url(self):
        api, transport = self.make_api([{
            "accounts": [
                {"mail_address": "info@example.invalid"},
                {"mail_address": "former@example.invalid"},
            ],
        }])
        self.assertEqual(
            ["former@example.invalid", "info@example.invalid"],
            api.list_mail_accounts("example.invalid"),
        )
        request, _ = transport.requests[0]
        self.assertEqual(
            "https://api.xserver.ne.jp/v1/server/server.example.invalid/mail?domain=example.invalid",
            request.full_url,
        )
        self.assertEqual("GET", request.get_method())

    def test_list_forwarding_addresses_uses_encoded_account_url(self):
        api, transport = self.make_api([{
            "forwarding_addresses": ["info@example.invalid", "outside@example.invalid"],
        }])
        self.assertEqual(
            ["info@example.invalid", "outside@example.invalid"],
            api.list_forwarding_addresses("former+tag@example.invalid"),
        )
        request, _ = transport.requests[0]
        self.assertEqual(
            "https://api.xserver.ne.jp/v1/server/server.example.invalid/mail/former%2Btag%40example.invalid/forwarding",
            request.full_url,
        )
        self.assertEqual("GET", request.get_method())

    def test_mail_account_and_forwarding_lists_reject_malformed_or_duplicate_data(self):
        invalid_account_responses = (
            {},
            {"accounts": {}},
            {"accounts": [{"mail_address": "not-an-address"}]},
            {"accounts": [{"mail_address": "first,second@example.invalid"}]},
            {"accounts": [{"mail_address": "a..b@example.invalid"}]},
            {"accounts": [{"mail_address": "info@example.invalid"}, {"mail_address": "info@example.invalid"}]},
        )
        for response in invalid_account_responses:
            with self.subTest(account_response=response):
                api, _ = self.make_api([response])
                with self.assertRaises(XServerApiError):
                    api.list_mail_accounts("example.invalid")

        invalid_forwarding_responses = (
            {},
            {"forwarding_addresses": {}},
            {"forwarding_addresses": ["not-an-address"]},
            {"forwarding_addresses": ["user@example.invalid,other"]},
            {"forwarding_addresses": ["a..b@example.invalid"]},
            {"forwarding_addresses": ["info@example.invalid", "info@example.invalid"]},
        )
        for response in invalid_forwarding_responses:
            with self.subTest(forwarding_response=response):
                api, _ = self.make_api([response])
                with self.assertRaises(XServerApiError):
                    api.list_forwarding_addresses("former@example.invalid")

    def test_discover_forwarding_sources_follows_reverse_paths_and_ignores_external_destinations(self):
        api, _ = self.make_api([
            {"accounts": [
                {"mail_address": "info@example.invalid"},
                {"mail_address": "former@example.invalid"},
                {"mail_address": "legacy@example.invalid"},
                {"mail_address": "cycle-a@example.invalid"},
                {"mail_address": "cycle-b@example.invalid"},
                {"mail_address": "external-only@example.invalid"},
            ]},
            {"forwarding_addresses": ["cycle-b@example.invalid"]},
            {"forwarding_addresses": ["cycle-a@example.invalid"]},
            {"forwarding_addresses": ["outside@example.invalid"]},
            {"forwarding_addresses": ["info@example.invalid"]},
            {"forwarding_addresses": []},
            {"forwarding_addresses": ["former@example.invalid"]},
        ])
        self.assertEqual(
            ["former@example.invalid", "info@example.invalid", "legacy@example.invalid"],
            api.discover_forwarding_sources("info@example.invalid"),
        )

    def test_default_transport_uses_verified_ca_context_and_keeps_hostname_checks(self):
        context = type("Context", (), {"verify_mode": 2, "check_hostname": True})()
        response = io.BytesIO(json.dumps({"filters": []}).encode())
        with patch("manager.xserver_api._build_verified_ssl_context", return_value=context), \
                patch("manager.xserver_api.urlopen", return_value=response) as opened:
            api = XServerApi(
                "server.example.invalid", "secret-api-key",
                "/home/example/private/mail-forward-command",
            )
            self.assertEqual([], api.list_filters())
        self.assertIs(opened.call_args.kwargs["context"], context)
        self.assertEqual(30, opened.call_args.kwargs["timeout"])

    def test_injected_transport_does_not_build_default_tls_context(self):
        transport = FakeTransport([{"filters": []}])
        with patch("manager.xserver_api._build_verified_ssl_context") as build_context:
            api = XServerApi(
                "server.example.invalid", "secret-api-key",
                "/home/example/private/mail-forward-command", transport=transport,
            )
            self.assertEqual([], api.list_filters())
        build_context.assert_not_called()

    def test_default_transport_fails_closed_before_network_when_ca_is_unavailable(self):
        with patch("manager.xserver_api._build_verified_ssl_context", side_effect=RuntimeError("CA unavailable")), \
                patch("manager.xserver_api.urlopen") as opened:
            api = XServerApi(
                "server.example.invalid", "secret-api-key",
                "/home/example/private/mail-forward-command",
            )
            with self.assertRaises(RuntimeError):
                api.list_filters()
        opened.assert_not_called()

    def test_command_target_uses_xserver_php85_pipe_format(self):
        api, _ = self.make_api([])
        self.assertEqual(
            "| /usr/bin/php8.5 /home/example/private/mail-forward-command",
            api.managed_command_target,
        )

    def test_command_path_must_be_one_absolute_script_path(self):
        for invalid in (
            "relative/script.php",
            "| /usr/bin/php8.5 /home/example/private/script.php",
            "/home/example/private/script with spaces.php",
            "/home/example/private/script.php\n--flag",
            "/home/example/private/script.php;touch",
            "/home/example/private/$script.php",
            "/home/example/private/../script.php",
            "/home/example/PUBLIC_HTML/script.php",
            "/home/example/Public_Html/script.php",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                XServerApi("server.example.invalid", "secret-api-key", invalid)

    def test_add_and_delete_send_official_payload_and_paths(self):
        rule = {
            "domain": "example.invalid",
            "conditions": [{"keyword": "me@example.invalid", "field": "header", "match_type": "contain"}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "method": "copy"},
        }
        api, transport = self.make_api([{"id": "new-id"}, {"message": "deleted"}])
        self.assertEqual("new-id", api.add_filter(rule)["id"])
        api.delete_filter("old id")
        add = transport.requests[0][0]
        delete = transport.requests[1][0]
        self.assertEqual(rule, json.loads(add.data))
        self.assertEqual("application/json", add.get_header("Content-type"))
        self.assertEqual("POST", add.get_method())
        self.assertEqual("DELETE", delete.get_method())
        self.assertTrue(delete.full_url.endswith("/mail-filter/old%20id"))

    def test_permission_and_rate_limit_errors_do_not_expose_api_key(self):
        api, _ = self.make_api([
            http_error(403, {"error": {"code": "FORBIDDEN", "message": "denied"}}),
        ])
        with self.assertRaises(XServerPermissionError) as caught:
            api.list_filters()
        self.assertNotIn("secret-api-key", str(caught.exception))

        api, _ = self.make_api([
            http_error(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}}, {"Retry-After": "12"}),
        ])
        with self.assertRaises(XServerRateLimitError) as caught:
            api.list_filters()
        self.assertEqual(12, caught.exception.retry_after)

    def test_managed_filter_requires_exact_recipient_shape_and_command(self):
        api, _ = self.make_api([])
        managed = {
            "domain": "example.invalid",
            "conditions": [{"keyword": "me@example.invalid", "field": "header", "match_type": "contain"}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "method": "copy"},
        }
        self.assertTrue(api.is_managed_filter(managed))
        for invalid_domain in (None, 7, "EXAMPLE.INVALID", "other.example.invalid"):
            changed = json.loads(json.dumps(managed))
            changed["domain"] = invalid_domain
            self.assertFalse(api.is_managed_filter(changed), invalid_domain)
        for mutation in (
            lambda r: r["action"].update(target="/tmp/other"),
            lambda r: r["conditions"].append({"keyword": "x", "field": "subject", "match_type": "contain"}),
            lambda r: r["conditions"][0].update(match_type="match"),
            lambda r: r["conditions"][0].update(field="to"),
            lambda r: r["action"].update(method="move"),
        ):
            changed = json.loads(json.dumps(managed))
            mutation(changed)
            self.assertFalse(api.is_managed_filter(changed))

    def test_managed_filter_rejects_keyword_that_is_not_one_complete_email_address(self):
        api, _ = self.make_api([])
        managed = {
            "domain": "example.invalid",
            "conditions": [{"keyword": "me@example.invalid", "field": "header", "match_type": "contain"}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "method": "copy"},
        }
        for keyword in (
            "me",
            "@example.invalid",
            "me@",
            "me@@example.invalid",
            "first,second@example.invalid",
            "user@example.invalid,other",
            "a..b@example.invalid",
            ".me@example.invalid",
            "me@example..invalid",
            "me@example.invalid\nBcc: attacker@example.invalid",
        ):
            changed = json.loads(json.dumps(managed))
            changed["conditions"][0]["keyword"] = keyword
            self.assertFalse(api.is_managed_filter(changed), keyword)

    def test_replace_deletes_only_after_added_rule_is_read_back(self):
        new_rule = {
            "domain": "example.invalid",
            "conditions": [{"keyword": "me@example.invalid", "field": "header", "match_type": "contain"}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "method": "copy"},
        }
        readback = dict(new_rule, id="new-id", priority=2)
        old_readback = dict(new_rule, id="old-id", priority=1)
        api, transport = self.make_api([
            {"filters": [old_readback]}, {"id": "new-id"},
            {"filters": [old_readback, readback]}, {"message": "deleted"},
        ])
        api.replace_managed_filter("old-id", new_rule)
        self.assertEqual(["GET", "POST", "GET", "DELETE"], [r.get_method() for r, _ in transport.requests])

    def test_replace_does_not_delete_when_readback_does_not_match(self):
        rule = {
            "domain": "example.invalid",
            "conditions": [{"keyword": "me@example.invalid", "field": "header", "match_type": "contain"}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "method": "copy"},
        }
        old = dict(rule, id="old-id")
        api, transport = self.make_api([{"filters": [old]}, {"id": "new-id"}, {"filters": []}])
        with self.assertRaises(RuntimeError):
            api.replace_managed_filter("old-id", rule)
        self.assertEqual(["GET", "POST", "GET"], [r.get_method() for r, _ in transport.requests])

    def test_replace_does_not_delete_unconfirmed_old_rule(self):
        rule = {
            "domain": "example.invalid",
            "conditions": [{"keyword": "me@example.invalid", "field": "header", "match_type": "contain"}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "method": "copy"},
        }
        new_readback = dict(rule, id="new-id")
        for old_readback in (
            None,
            dict(rule, id="old-id", domain="other.example.invalid"),
            dict(rule, id="old-id", action={"type": "mail_address", "target": "/tmp/other", "method": "copy"}),
        ):
            filters = [new_readback] + ([old_readback] if old_readback else [])
            api, transport = self.make_api([{"filters": filters}])
            with self.assertRaises(RuntimeError):
                api.replace_managed_filter("old-id", rule)
            self.assertEqual(["GET"], [r.get_method() for r, _ in transport.requests])

    def test_cross_domain_replace_confirms_old_before_add_then_confirms_new_before_delete(self):
        old_rule = {
            "id": "old-id", "domain": "old.example.invalid",
            "conditions": [{"keyword": "user@old." + "example.invalid", "field": "header", "match_type": "contain"}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "method": "copy"},
        }
        new_rule = {
            "domain": "new.example.invalid",
            "conditions": [{"keyword": "user@new." + "example.invalid", "field": "header", "match_type": "contain"}],
            "action": old_rule["action"],
        }
        api, transport = self.make_api([
            {"filters": [old_rule]}, {"id": "new-id"},
            {"filters": [dict(new_rule, id="new-id")]}, {"message": "deleted"},
        ])
        api.replace_managed_filter("old-id", new_rule, old_domain="old.example.invalid")
        self.assertEqual(["GET", "POST", "GET", "DELETE"], [r.get_method() for r, _ in transport.requests])
        self.assertTrue(transport.requests[0][0].full_url.endswith("?domain=old.example.invalid"))
        self.assertTrue(transport.requests[2][0].full_url.endswith("?domain=new.example.invalid"))

    def test_snapshot_filters_returns_canonical_full_set_with_ids_and_duplicates(self):
        first = {
            "id": "z", "domain": "two.example.invalid", "priority": 4,
            "conditions": [{"match_type": "match", "field": "to", "keyword": "b@example.invalid"}],
            "action": {"method": "copy", "target": "| /usr/bin/php8.5 /home/example/private/mail-forward-command", "type": "mail_address"},
        }
        second = dict(first, id="a", domain="one.example.invalid")
        api, transport = self.make_api([{"filters": [first, second, dict(second, id="b")]}])
        snapshot = api.snapshot_filters()
        self.assertEqual(["a", "b", "z"], [item["id"] for item in snapshot])
        self.assertEqual("GET", transport.requests[0][0].get_method())
        self.assertNotIn("?domain=", transport.requests[0][0].full_url)
        self.assertEqual(snapshot, json.loads(XServerApi.canonical_filter_snapshot(snapshot)))

    def test_snapshot_filters_rejects_non_list_or_rule_without_string_id(self):
        for response in ({"filters": {}}, {"filters": [{"domain": "one.example.invalid"}]}):
            api, _ = self.make_api([response])
            with self.assertRaises(XServerApiError):
                api.snapshot_filters()


if __name__ == "__main__":
    unittest.main()
