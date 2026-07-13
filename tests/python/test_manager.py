import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from manager.manage import (MailManager, _ScopedMigrationApi, _filesystem_config_path,
                            _legacy_bootstrap_asset_paths, main, mask_webhook_url,
                            validate_email)
from manager.email_address import CanonicalEmailError, canonical_email, canonical_email_list
from manager.private_config_ssh import ConfigCasResult
from manager.release_deployer import ReleaseDeployer
from manager.scope_journal import ScopeJournal
from tests.python.test_release_deployer import FakeFtps as MigrationFtps, FakeValidator, MigrationApi


ADDRESS_A = "alpha@example.invalid"
ADDRESS_B = "beta@example.invalid"
ADDRESS_C = "gamma@example.invalid"


class CanonicalEmailTest(unittest.TestCase):
    def test_preserves_local_and_lowercases_domain(self):
        self.assertEqual(
            "CaseSensitive@example.invalid",
            canonical_email("CaseSensitive@EXAMPLE.INVALID"),
        )
        self.assertEqual(
            ["A@example.invalid", "a@example.invalid"],
            canonical_email_list(
                ["a@EXAMPLE.INVALID", "A@example.invalid"], allow_empty=False
            ),
        )

    def test_rejects_noncanonical_or_invalid_inputs(self):
        invalid = [
            "Name <x@example.invalid>", " x@example.invalid", "x@example.invalid ",
            "x\r@example.invalid", "x\n@example.invalid", ".x@example.invalid",
            "x..y@example.invalid", "x" * 65 + "@example.invalid",
            "x@-example.invalid", "x@example-.invalid", "x@exa_mple.invalid",
            "x@" + ("a." * 126) + "aaa", "x@" + ("a." * 126) + "aaaa",
            True, False, None, 1,
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CanonicalEmailError):
                canonical_email(value)

    def test_lists_require_lists_and_can_reject_noncanonical_duplicates(self):
        for value in (None, "x@example.invalid", (), True):
            with self.subTest(value=value), self.assertRaises(CanonicalEmailError):
                canonical_email_list(value, allow_empty=False)
        with self.assertRaises(CanonicalEmailError):
            canonical_email_list([], allow_empty=False)
        self.assertEqual([], canonical_email_list([], allow_empty=True))
        self.assertEqual(
            ["A@example.invalid"],
            canonical_email_list(
                ["A@EXAMPLE.INVALID", "A@example.invalid"], allow_empty=False
            ),
        )
        for values in (["A@EXAMPLE.INVALID"], ["b@example.invalid", "a@example.invalid"],
                       ["a@example.invalid", "a@example.invalid"]):
            with self.subTest(values=values), self.assertRaises(CanonicalEmailError):
                canonical_email_list(
                    values, allow_empty=False, reject_duplicates=True
                )


class FakeApi:
    def __init__(self, filters=()):
        self.filters = list(filters)
        self.added = []
        self.deleted = []
        self.events = []
        self.discovered = None

    def list_filters(self, domain=None):
        self.events.append(("list", domain))
        return [item for item in self.filters if domain is None or item.get("domain") == domain]

    def snapshot_filters(self):
        self.events.append(("snapshot",))
        return json.loads(json.dumps(self.filters))

    def discover_forwarding_sources(self, base_address):
        self.events.append(("discover", base_address))
        return list(self.discovered if self.discovered is not None else [base_address])

    def is_managed_filter(self, rule):
        conditions = rule.get("conditions", [])
        keyword = conditions[0].get("keyword") if len(conditions) == 1 else ""
        return (len(conditions) == 1 and conditions[0].get("field") == "header"
                and conditions[0].get("match_type") == "contain"
                and rule.get("domain") == keyword.rsplit("@", 1)[-1]
                and rule.get("action", {}).get("target") == "| /usr/bin/php8.5 /private/mail-forward-command")

    def add_filter(self, rule):
        self.added.append(rule)
        new_id = "new-%d" % len(self.added)
        self.events.append(("add", rule["domain"]))
        self.filters.append(dict(rule, id=new_id, managed=True))
        return {"id": new_id}

    def delete_filter(self, filter_id):
        self.events.append(("delete", filter_id))
        self.deleted.append(filter_id)
        self.filters = [rule for rule in self.filters if rule.get("id") != filter_id]

    def replace_managed_filter(self, old_id, new_rule, *, old_domain=None):
        old = [rule for rule in self.list_filters(old_domain) if rule.get("id") == old_id and self.is_managed_filter(rule)]
        if not old:
            raise RuntimeError("old managed filter was not confirmed by readback")
        new_id = self.add_filter(new_rule)["id"]
        new = [rule for rule in self.list_filters(new_rule["domain"]) if rule.get("id") == new_id]
        if not new:
            raise RuntimeError("new filter was not confirmed by readback")
        self.delete_filter(old_id)
        return new_id


class FakeDeployer:
    def __init__(self, config=None, read_error=None, read_configs=None):
        self.configs = []
        self.config = dict(config or {})
        self.read_error = read_error
        self.read_configs = iter(read_configs) if read_configs is not None else None

    def read_private_config(self):
        if self.read_error:
            raise self.read_error
        if self.read_configs is not None:
            return dict(next(self.read_configs))
        return dict(self.config)

    def update_private_config(self, config):
        self.configs.append(config)
        self.config = dict(config)

    @staticmethod
    def _digest(config):
        return __import__("hashlib").sha256(json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def read(self):
        config = self.read_private_config()
        self._last_read = dict(config)
        return config, self._digest(config)

    def compare_and_swap(self, expected_sha256, updated):
        current = getattr(self, "_last_read", self.config)
        current_sha256 = self._digest(current)
        if current_sha256 != expected_sha256:
            return ConfigCasResult("conflict", current_sha256, current_sha256)
        self.update_private_config(updated)
        new_sha256 = self._digest(updated)
        self._last_read = dict(updated)
        return ConfigCasResult("changed", current_sha256, new_sha256)


class FakePrivateConfigClient:
    def __init__(self, config, *, result=None, readbacks=None, read_error=None):
        self.config = dict(config)
        self.sha256 = "a" * 64
        self.result = result or ConfigCasResult("changed", self.sha256, "b" * 64)
        self.readbacks = iter(readbacks) if readbacks is not None else None
        self.read_error = read_error
        self.read_calls = 0
        self.cas_inputs = []

    def read(self):
        self.read_calls += 1
        if self.read_error is not None and self.read_calls > 1:
            raise self.read_error
        if self.readbacks is not None:
            config, sha256 = next(self.readbacks)
            return dict(config), sha256
        if self.read_calls > 1 and self.result.status in {"changed", "unchanged"}:
            return dict(self.cas_inputs[-1]), self.result.new_sha256
        return dict(self.config), self.sha256

    def compare_and_swap(self, expected_sha256, updated):
        self.cas_inputs.append(dict(updated))
        self.expected_sha256 = expected_sha256
        return self.result


class FakeManagerConfigClient:
    def __init__(self, deployer):
        self.deployer = deployer
        self.last = None
        self.last_sha256 = None

    @staticmethod
    def digest(config):
        return __import__("hashlib").sha256(json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def read(self):
        if self.deployer.read_error:
            raise self.deployer.read_error
        if self.deployer.read_configs is not None:
            config = dict(next(self.deployer.read_configs))
        else:
            config = dict(self.deployer.config)
        self.last = config
        self.last_sha256 = self.digest(config)
        return dict(config), self.last_sha256

    def compare_and_swap(self, expected_sha256, updated):
        current = dict(self.deployer.config if self.last is None else self.last)
        current_sha256 = self.digest(current)
        if current_sha256 != expected_sha256:
            return ConfigCasResult("conflict", current_sha256, current_sha256)
        self.deployer.configs.append(dict(updated))
        self.deployer.config = dict(updated)
        new_sha256 = self.digest(updated)
        self.last = dict(updated)
        self.last_sha256 = new_sha256
        return ConfigCasResult("changed", current_sha256, new_sha256)


def managed(filter_id, address):
    return {
        "id": filter_id,
        "domain": address.split("@", 1)[1],
        "conditions": [{"field": "header", "match_type": "contain", "keyword": address}],
        "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /private/mail-forward-command", "method": "copy"},
        "managed": True,
    }


def legacy_managed(filter_id, address):
    rule = managed(filter_id, address)
    rule["conditions"] = [{"field": "to", "match_type": "match", "keyword": address}]
    return rule


class ManagerTest(unittest.TestCase):
    def test_pre_release_config_upgrade(self):
        legacy = {
            "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
            "error_recipients": ["z@EXAMPLE.INVALID", "A@example.invalid",
                                 "z@example.invalid"],
            "notification_targets": ["b@EXAMPLE.INVALID", "a@example.invalid"],
            "log_path": "/home/example/private/notifier.jsonl",
            "unknown": {"preserved": True},
        }
        client = FakePrivateConfigClient(legacy)
        manager = MailManager(
            FakeApi(), FakeDeployer(), "/private/mail-forward-command",
            config=legacy, private_config_client=client, output_fn=lambda _message: None,
        )
        with patch("manager.manage.secrets.token_bytes", return_value=b"k" * 32):
            self.assertTrue(manager.ensure_runtime_config())
            self.assertTrue(manager.ensure_runtime_config())
        self.assertEqual(1, len(client.cas_inputs))
        upgraded = client.cas_inputs[0]
        self.assertEqual(["A@example.invalid", "z@example.invalid"],
                         upgraded["error_recipients"])
        self.assertEqual(["a@example.invalid", "b@example.invalid"],
                         upgraded["notification_targets"])
        self.assertEqual([], upgraded["notification_pinned_targets"])
        self.assertEqual({"preserved": True}, upgraded["unknown"])
        self.assertEqual("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
                         upgraded["system_mail_hmac_key"])

    def test_pre_release_config_bounds_stop_before_mutation(self):
        def addresses(count, to_bytes=None):
            local_lengths = [2] * count
            if to_bytes is not None:
                # example.invalid plus @ is 16 bytes; signed To uses comma only.
                local_total = to_bytes - count * 16 - max(0, count - 1)
                local_lengths = [10] * (count - 1)
                local_lengths.append(local_total - sum(local_lengths))
            return [
                (chr(97 + (index // 26)) + chr(97 + (index % 26))
                 + "x" * (length - 2))
                + "@example.invalid"
                for index, length in enumerate(local_lengths)
            ]

        cases = [
            ("count-32", addresses(32), "/home/example/private/notifier.jsonl", True, None),
            ("count-33", addresses(33), "/home/example/private/notifier.jsonl", False,
             "エラー通知先は合計32件以下に減らしてから再実行してください。"),
            ("to-900", addresses(32, 900), "/home/example/private/notifier.jsonl", True, None),
            ("to-901", addresses(32, 901), "/home/example/private/notifier.jsonl", False,
             "エラー通知先の合計長を900バイト以下に減らしてから再実行してください。"),
            ("log-4096", [ADDRESS_A], "/" + "a" * 4095, True, None),
            ("log-4097", [ADDRESS_A], "/" + "a" * 4096, False, None),
        ]
        class NoMutationWorkflow:
            def __init__(self):
                self.stage_calls = []
                self.switch_calls = []
            def stage(self, *args):
                self.stage_calls.append(args)
                return {}
            def switch(self, *args):
                self.switch_calls.append(args)

        for name, recipients, log_path, accepted, expected_message in cases:
            with self.subTest(name=name):
                legacy = {
                    "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
                    "error_recipients": recipients, "notification_targets": [],
                    "log_path": log_path, "unknown": "keep",
                }
                client = FakePrivateConfigClient(legacy)
                workflow = NoMutationWorkflow()
                deploy_answers = iter(["/candidate", "release-bounds", "検証配備する"])
                manager = MailManager(
                    FakeApi(), FakeDeployer(), "/private/mail-forward-command",
                    config=legacy, private_config_client=client,
                    release_workflow=workflow,
                    input_fn=lambda _prompt="": next(deploy_answers),
                    output_fn=lambda _message: None,
                )
                if accepted:
                    with patch("manager.manage.secrets.token_bytes", return_value=b"k" * 32):
                        self.assertTrue(manager.ensure_runtime_config())
                    sizes = MailManager._runtime_config_sizes(client.cas_inputs[0])
                    self.assertLess(sizes["header_line_bytes"], 998)
                    self.assertLess(sizes["signed_message_bytes"], 65536)
                    self.assertEqual("keep", client.cas_inputs[0]["unknown"])
                else:
                    with patch("manager.manage.secrets.token_bytes") as generated:
                        with patch.object(Path, "is_file", return_value=True), \
                                self.assertRaises(RuntimeError) as raised:
                            manager.deploy_release()
                    if expected_message is not None:
                        self.assertEqual(expected_message, str(raised.exception))
                    else:
                        self.assertRegex(str(raised.exception), "管理者|減らして|変更")
                    generated.assert_not_called()
                    self.assertEqual([], client.cas_inputs)
                    self.assertEqual([], manager.api.events)
                    self.assertEqual([], workflow.stage_calls)
                    self.assertEqual([], workflow.switch_calls)

    def test_pre_release_bounds_do_not_count_notification_targets_as_error_recipients(self):
        targets = [f"target{index:02d}@example.invalid" for index in range(40)]
        legacy = {
            "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
            "error_recipients": [ADDRESS_A],
            "notification_targets": targets,
            "notification_pinned_targets": targets,
            "log_path": "/home/example/private/notifier.jsonl",
        }
        client = FakePrivateConfigClient(legacy)
        manager = MailManager(
            FakeApi(), FakeDeployer(), "/private/mail-forward-command",
            config=legacy, private_config_client=client,
            release_workflow=None,
            input_fn=lambda _prompt="": "検証配備する",
            output_fn=lambda _message: None,
        )
        with patch("manager.manage.secrets.token_bytes", return_value=b"k" * 32):
            self.assertTrue(manager.ensure_runtime_config())
        self.assertEqual(targets, client.cas_inputs[0]["notification_targets"])

    def test_pre_release_config_conflict_or_bad_readback_stops_before_stage(self):
        legacy = {
            "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
            "error_recipients": [ADDRESS_A], "notification_targets": [],
            "log_path": "/home/example/private/notifier.jsonl", "unknown": "keep",
        }

        class Workflow:
            def __init__(self):
                self.stages = []
            def stage(self, *args):
                self.stages.append(args)
                return {}

        with tempfile.TemporaryDirectory() as source:
            canonical = Path(source) / "src/CanonicalEmail.php"
            canonical.parent.mkdir()
            canonical.write_text("<?php\n")
            for name, client in (
                ("conflict", FakePrivateConfigClient(
                    legacy, result=ConfigCasResult("conflict", "a" * 64, "a" * 64)
                )),
                ("bad-readback", FakePrivateConfigClient(
                    legacy, result=ConfigCasResult("changed", "a" * 64, "b" * 64),
                    readbacks=[(legacy, "a" * 64), ({**legacy, "unknown": "raced"}, "b" * 64)],
                )),
            ):
                workflow = Workflow()
                answers = iter([source, "release-review", "検証配備する"])
                api = FakeApi()
                manager = MailManager(
                    api, FakeDeployer(), "/private/mail-forward-command",
                    config=legacy, private_config_client=client,
                    release_workflow=workflow, input_fn=lambda _prompt="": next(answers),
                    output_fn=lambda _message: None,
                )
                with self.subTest(name=name), patch(
                    "manager.manage.secrets.token_bytes", return_value=b"k" * 32
                ), self.assertRaises(RuntimeError):
                    manager.deploy_release()
                self.assertEqual([], workflow.stages)
                self.assertEqual([], api.events)

    def test_error_recipient_mutations_remain_canonical(self):
        key = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"
        base = {
            "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
            "notification_targets": [ADDRESS_C],
            "notification_pinned_targets": [], "system_mail_hmac_key": key,
            "log_path": "/home/example/private/notifier.jsonl",
            "unknown": {"keep": True},
        }
        cases = [
            ("add", [ADDRESS_B], ["A@EXAMPLE.INVALID," + ADDRESS_B, "配備する"],
             "add_error_recipients", ["A@example.invalid", ADDRESS_B]),
            ("change", ["A@example.invalid", ADDRESS_B],
             [ADDRESS_B, "C@EXAMPLE.INVALID", "変更して配備する"],
             "change_error_recipient", ["A@example.invalid", "C@example.invalid"]),
            ("delete", ["A@example.invalid", ADDRESS_B],
             ["beta@EXAMPLE.INVALID", "削除して配備する"],
             "delete_error_recipient", ["A@example.invalid"]),
        ]
        for name, current, responses, action, expected_recipients in cases:
            with self.subTest(name=name):
                config = {**base, "error_recipients": current}
                expected = {**config, "error_recipients": expected_recipients}
                client = FakePrivateConfigClient(
                    config, result=ConfigCasResult("changed", "a" * 64, "b" * 64),
                    readbacks=[(config, "a" * 64), (config, "a" * 64),
                               (expected, "b" * 64)],
                )
                answers = iter(responses)
                manager = MailManager(
                    FakeApi(), FakeDeployer(), "/private/mail-forward-command",
                    config=config, error_recipients=current,
                    private_config_client=client,
                    input_fn=lambda _prompt="": next(answers),
                    output_fn=lambda _message: None,
                )
                self.assertTrue(getattr(manager, action)())
                self.assertEqual(expected, client.cas_inputs[0])
                self.assertEqual(key, client.cas_inputs[0]["system_mail_hmac_key"])
                self.assertEqual({"keep": True}, client.cas_inputs[0]["unknown"])

    def test_error_recipient_mutation_conflict_and_bad_readback_stop_safely(self):
        key = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"
        base = {
            "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
            "notification_targets": [ADDRESS_C], "notification_pinned_targets": [],
            "system_mail_hmac_key": key,
            "log_path": "/home/example/private/notifier.jsonl",
            "unknown": {"keep": True},
        }
        actions = [
            ("add", [ADDRESS_B], ["A@EXAMPLE.INVALID", "配備する"],
             "add_error_recipients"),
            ("change", ["A@example.invalid", ADDRESS_B],
             [ADDRESS_B, "C@EXAMPLE.INVALID", "変更して配備する"],
             "change_error_recipient"),
            ("delete", ["A@example.invalid", ADDRESS_B],
             ["beta@EXAMPLE.INVALID", "削除して配備する"],
             "delete_error_recipient"),
        ]

        class NoReleaseMutation:
            def __init__(self):
                self.stage_calls = []
                self.switch_calls = []
            def stage(self, *args):
                self.stage_calls.append(args)
            def switch(self, *args):
                self.switch_calls.append(args)

        for action_name, current, responses, method_name in actions:
            config = {**base, "error_recipients": current}
            for failure in ("conflict", "bad-readback"):
                with self.subTest(action=action_name, failure=failure):
                    result = (ConfigCasResult("conflict", "a" * 64, "a" * 64)
                              if failure == "conflict" else
                              ConfigCasResult("changed", "a" * 64, "b" * 64))
                    readbacks = [(config, "a" * 64), (config, "a" * 64)]
                    if failure == "bad-readback":
                        readbacks.append(({**config, "unknown": {"raced": True}}, "b" * 64))
                    client = FakePrivateConfigClient(
                        config, result=result, readbacks=readbacks
                    )
                    answers = iter(responses)
                    api = FakeApi()
                    workflow = NoReleaseMutation()
                    manager = MailManager(
                        api, FakeDeployer(), "/private/mail-forward-command",
                        config=config, error_recipients=current,
                        private_config_client=client, release_workflow=workflow,
                        input_fn=lambda _prompt="": next(answers),
                        output_fn=lambda _message: None,
                    )
                    if failure == "conflict":
                        self.assertFalse(getattr(manager, method_name)())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "変更を確認"):
                            getattr(manager, method_name)()
                    self.assertEqual(config, manager.config)
                    self.assertEqual(config, client.config)
                    self.assertEqual([], api.events)
                    self.assertEqual([], workflow.stage_calls)
                    self.assertEqual([], workflow.switch_calls)

    OLD_URL = "https://webhook.worksmobile.com/message/old-secret-token"
    NEW_URL = "https://webhook.worksmobile.com/message/new-secret-token"

    def make_webhook_manager(self, *, answers=(), status=200, result=None, readbacks=None,
                             read_error=None, sender_error=None):
        config = {"webhook_url": self.OLD_URL, "unknown": {"preserved": True}}
        private = FakePrivateConfigClient(
            config, result=result, readbacks=readbacks, read_error=read_error)
        sent = []
        def sender(url, payload):
            sent.append((url, payload))
            if sender_error is not None:
                raise sender_error
            return status
        output = []
        answers = iter(answers)
        manager = MailManager(
            FakeApi(), FakeDeployer(config), "/private/mail-forward-command",
            input_fn=lambda prompt="": next(answers), output_fn=output.append, config=config,
            private_config_client=private, webhook_sender=sender,
        )
        return manager, private, sent, output

    def test_menu_preserves_13_and_adds_webhook_actions(self):
        self.assertIn("13. 通知対象を転送設定から同期", MailManager.MENU)
        self.assertIn("14. Webhook URL確認", MailManager.MENU)
        self.assertIn("15. Webhook URL変更", MailManager.MENU)

    def test_manager_production_actions_have_no_ftps_config_read_or_update_calls(self):
        source = (Path(__file__).parents[2] / "manager/manage.py").read_text()
        self.assertNotIn("self.deployer.read_private_config", source)
        self.assertNotIn("self.deployer.update_private_config", source)

    def test_mask_never_exposes_short_or_long_token(self):
        for token in ("a", "abcd", "very-long-secret-token"):
            value = "https://webhook.worksmobile.com/message/" + token
            masked = mask_webhook_url(value)
            self.assertNotIn("/message/" + token, masked)
            self.assertTrue(masked.startswith("https://webhook.worksmobile.com/message/"))

    def test_mask_uses_stars_only_for_short_tokens_and_last_four_for_long_tokens(self):
        base = "https://webhook.worksmobile.com/message/"
        self.assertEqual(base + "*", mask_webhook_url(base + "a"))
        self.assertEqual(base + "****", mask_webhook_url(base + "abcd"))
        self.assertEqual(base + "********…5678", mask_webhook_url(base + "token-12345678"))

    def test_show_webhook_masks_by_default_and_reveals_once_only_for_exact_phrase(self):
        manager, private, _, output = self.make_webhook_manager(answers=[""])
        manager.show_webhook_url()
        self.assertNotIn(self.OLD_URL, "\n".join(output))
        self.assertEqual(1, private.read_calls)
        manager, _, _, output = self.make_webhook_manager(answers=["Webhook URLを表示する"])
        manager.show_webhook_url()
        self.assertEqual(1, "\n".join(output).count(self.OLD_URL))

    def test_change_requires_exact_confirmation_and_preserves_unknown_keys(self):
        manager, private, sent, output = self.make_webhook_manager(
            answers=[self.NEW_URL, "Webhook URLを変更する"])
        self.assertTrue(manager.change_webhook_url())
        expected = {"webhook_url": self.NEW_URL, "unknown": {"preserved": True}}
        self.assertEqual([expected], private.cas_inputs)
        self.assertEqual(2, private.read_calls)
        self.assertEqual(self.NEW_URL, sent[0][0])
        self.assertEqual({"title": "Webhook URL変更テスト",
                          "body": {"text": "Mac管理アプリからの接続確認です。"}}, sent[0][1])
        rendered = "\n".join(output)
        self.assertNotIn(self.NEW_URL, rendered)
        self.assertNotIn(self.OLD_URL, rendered)

    def test_change_cancellation_never_sends_or_updates(self):
        manager, private, sent, _ = self.make_webhook_manager(answers=[self.NEW_URL, "はい"])
        self.assertFalse(manager.change_webhook_url())
        self.assertEqual([], sent)
        self.assertEqual([], private.cas_inputs)

    def test_change_rejects_noncanonical_url_before_network(self):
        invalid = "https://webhook.worksmobile.com:443/message/token"
        manager, private, sent, _ = self.make_webhook_manager(answers=[invalid])
        with self.assertRaises(RuntimeError):
            manager.change_webhook_url()
        self.assertEqual([], sent)
        self.assertEqual([], private.cas_inputs)

    def test_change_hostile_port_never_retains_secret_in_exception_chain(self):
        secret = "secret-token"
        hostile = "https://webhook.worksmobile.com:" + secret + "/message/x"
        manager, private, sent, _ = self.make_webhook_manager(answers=[hostile])
        with self.assertRaises(RuntimeError) as caught:
            manager.change_webhook_url()
        pending = [caught.exception]
        seen = set()
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            self.assertNotIn(secret, str(error))
            pending.extend(item for item in (error.__cause__, error.__context__)
                           if item is not None)
        self.assertEqual([], sent)
        self.assertEqual([], private.cas_inputs)

    def test_change_requires_exact_http_200_before_cas(self):
        for status in (201, 204, 400, None, "200", True):
            with self.subTest(status=status):
                manager, private, _, _ = self.make_webhook_manager(
                    answers=[self.NEW_URL, "Webhook URLを変更する"], status=status)
                with self.assertRaises(RuntimeError):
                    manager.change_webhook_url()
                self.assertEqual([], private.cas_inputs)

    def test_run_dispatches_webhook_menu_actions_14_and_15(self):
        changed = {"webhook_url": self.NEW_URL, "unknown": {"preserved": True}}
        manager, private, sent, output = self.make_webhook_manager(
            answers=["14", "", "15", self.NEW_URL, "Webhook URLを変更する", "0"],
            readbacks=[
                ({"webhook_url": self.OLD_URL, "unknown": {"preserved": True}}, "a" * 64),
                ({"webhook_url": self.OLD_URL, "unknown": {"preserved": True}}, "a" * 64),
                (changed, "b" * 64),
            ])
        manager.run()
        self.assertEqual(3, private.read_calls)
        self.assertEqual(1, len(private.cas_inputs))
        self.assertEqual(1, len(sent))
        self.assertIn("Webhook URLの変更を確認しました。", output)

    def test_change_timeout_is_redacted_and_never_calls_cas(self):
        manager, private, _, output = self.make_webhook_manager(
            answers=[self.NEW_URL, "Webhook URLを変更する"],
            sender_error=TimeoutError(self.NEW_URL))
        with self.assertRaises(RuntimeError) as caught:
            manager.change_webhook_url()
        self.assertNotIn(self.NEW_URL, str(caught.exception))
        self.assertNotIn(self.NEW_URL, "\n".join(output))
        self.assertEqual([], private.cas_inputs)

    def test_change_fails_redacted_for_all_non_success_cas_statuses(self):
        for status in ("conflict", "restored"):
            with self.subTest(status=status):
                result = ConfigCasResult(status, "a" * 64, "a" * 64)
                manager, private, _, output = self.make_webhook_manager(
                    answers=[self.NEW_URL, "Webhook URLを変更する"], result=result)
                with self.assertRaises(RuntimeError) as caught:
                    manager.change_webhook_url()
                self.assertEqual(1, private.read_calls)
                self.assertNotIn(self.NEW_URL, str(caught.exception) + "\n".join(output))

    def test_change_accepts_unchanged_only_after_matching_fresh_readback(self):
        result = ConfigCasResult("unchanged", "a" * 64, "b" * 64)
        manager, private, _, _ = self.make_webhook_manager(
            answers=[self.NEW_URL, "Webhook URLを変更する"], result=result)
        self.assertTrue(manager.change_webhook_url())
        self.assertEqual(2, private.read_calls)

    def test_change_rejects_readback_hash_or_url_mismatch_without_secret(self):
        for config, sha256 in (({"webhook_url": self.NEW_URL}, "c" * 64),
                               ({"webhook_url": self.OLD_URL}, "b" * 64)):
            with self.subTest(config=config, sha256=sha256):
                readbacks = [({"webhook_url": self.OLD_URL}, "a" * 64), (config, sha256)]
                manager, _, _, output = self.make_webhook_manager(
                    answers=[self.NEW_URL, "Webhook URLを変更する"], readbacks=readbacks)
                with self.assertRaises(RuntimeError) as caught:
                    manager.change_webhook_url()
                rendered = str(caught.exception) + "\n" + "\n".join(output)
                self.assertNotIn(self.NEW_URL, rendered)
                self.assertNotIn(self.OLD_URL, rendered)

    def test_change_post_cas_read_failure_is_redacted(self):
        manager, _, _, output = self.make_webhook_manager(
            answers=[self.NEW_URL, "Webhook URLを変更する"],
            read_error=OSError(self.NEW_URL))
        with self.assertRaises(RuntimeError) as caught:
            manager.change_webhook_url()
        self.assertNotIn(self.NEW_URL, str(caught.exception) + "\n".join(output))

    def make_sync_manager(self, *, answers=(), filters=(), expected=(), config=None,
                          read_configs=None):
        config = config or {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A],
            "command_path": "/private/mail-forward-command",
        }
        manager, api, deployer, output = self.make_manager(
            answers=answers, filters=filters, config=config, read_configs=read_configs,
        )
        api.discovered = list(expected)
        return manager, api, deployer, output

    def test_target_sync_diagnostics_report_drift_without_printing_addresses(self):
        manager, api, _, output = self.make_sync_manager(
            filters=[managed("base", ADDRESS_A)], expected=[ADDRESS_A, ADDRESS_B],
        )
        self.assertEqual([ADDRESS_A, ADDRESS_B], manager.expected_targets())
        plan = manager.plan_target_sync()
        self.assertEqual([ADDRESS_B], plan["add"])
        manager.show_diagnostics()
        self.assertIn("通知対象: 一致（API 1件 / リモート設定 1件）", output)
        self.assertNotIn(ADDRESS_A, "\n".join(output))
        self.assertNotIn(ADDRESS_B, "\n".join(output))
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))

    def test_target_sync_requires_exact_confirmation_without_writes(self):
        manager, api, deployer, output = self.make_sync_manager(
            answers=["はい"], filters=[managed("base", ADDRESS_A)],
            expected=[ADDRESS_A, ADDRESS_B],
        )
        self.assertFalse(manager.sync_targets())
        self.assertIn("+ " + ADDRESS_B, output)
        self.assertEqual([], deployer.configs)
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))

    def test_target_sync_adds_and_reads_back_before_deleting_stale_then_updates_config(self):
        manager, api, deployer, _ = self.make_sync_manager(
            answers=["通知対象を同期する"],
            filters=[managed("base", ADDRESS_A), managed("stale", ADDRESS_C)],
            expected=[ADDRESS_A, ADDRESS_B],
        )
        self.assertTrue(manager.sync_targets())
        add_index = next(i for i, event in enumerate(api.events) if event[0] == "add")
        delete_index = next(i for i, event in enumerate(api.events) if event[0] == "delete")
        self.assertIn(("snapshot",), api.events[add_index + 1:delete_index])
        self.assertEqual([ADDRESS_A, ADDRESS_B], deployer.configs[-1]["notification_targets"])

    def test_target_sync_readback_mismatch_leaves_stale_and_config_untouched(self):
        manager, api, deployer, _ = self.make_sync_manager(
            answers=["通知対象を同期する"],
            filters=[managed("base", ADDRESS_A), managed("stale", ADDRESS_C)],
            expected=[ADDRESS_A, ADDRESS_B],
        )
        original_add = api.add_filter
        def add_without_readback(rule):
            result = original_add(rule)
            api.filters = [item for item in api.filters if item.get("id") != result["id"]]
            return result
        api.add_filter = add_without_readback
        with self.assertRaises(RuntimeError):
            manager.sync_targets()
        self.assertNotIn("stale", api.deleted)
        self.assertEqual([], deployer.configs)

    def test_target_sync_changed_remote_config_aborts_before_api_write(self):
        snapshot = {"notification_base_address": ADDRESS_A,
                    "notification_targets": [ADDRESS_A], "unknown": 1}
        changed = dict(snapshot, unknown=2)
        manager, api, deployer, output = self.make_sync_manager(
            filters=[managed("base", ADDRESS_A)], expected=[ADDRESS_A, ADDRESS_B],
            config=snapshot, read_configs=[changed],
        )
        self.assertFalse(manager.sync_targets())
        self.assertIn("競合", "\n".join(output))
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))
        self.assertEqual([], deployer.configs)

    def test_target_sync_remote_config_race_after_api_reconciliation_never_uploads(self):
        snapshot = {"notification_base_address": ADDRESS_A,
                    "notification_targets": [ADDRESS_A], "unknown": 1}
        raced = dict(snapshot, unknown=2)
        manager, api, deployer, _ = self.make_sync_manager(
            answers=["通知対象を同期する"], filters=[managed("base", ADDRESS_A)],
            expected=[ADDRESS_A, ADDRESS_B], config=snapshot,
            read_configs=[snapshot, snapshot, raced],
        )
        with self.assertRaisesRegex(RuntimeError, "変更"):
            manager.sync_targets()
        self.assertTrue(any(event[0] == "add" for event in api.events))
        self.assertEqual([], deployer.configs)

    def test_target_sync_preserves_unrelated_rule_byte_for_byte(self):
        unrelated = {"id": "keep", "domain": "example.invalid", "opaque": [1, {"x": True}]}
        manager, api, _, _ = self.make_sync_manager(
            answers=["通知対象を同期する"],
            filters=[managed("base", ADDRESS_A), unrelated], expected=[ADDRESS_A, ADDRESS_B],
        )
        before = json.dumps(unrelated, sort_keys=True)
        self.assertTrue(manager.sync_targets())
        self.assertEqual(before, json.dumps(next(r for r in api.filters if r["id"] == "keep"), sort_keys=True))

    def test_target_sync_migrates_same_address_legacy_and_preserves_cross_domain_rule(self):
        legacy = legacy_managed("legacy", ADDRESS_A)
        cross_domain = legacy_managed("keep", ADDRESS_B)
        cross_domain["domain"] = "other.example.invalid"
        manager, api, _, _ = self.make_sync_manager(
            answers=["通知対象を同期する"], filters=[legacy, cross_domain],
            expected=[ADDRESS_A],
        )
        before = json.dumps(cross_domain, sort_keys=True)
        self.assertTrue(manager.sync_targets())
        self.assertIn("legacy", api.deleted)
        self.assertNotIn("keep", api.deleted)
        self.assertEqual(before, json.dumps(next(r for r in api.filters if r["id"] == "keep"), sort_keys=True))
        self.assertTrue(any(r["conditions"][0]["field"] == "header" for r in api.filters))

    def test_target_sync_adds_all_new_rules_before_retiring_all_legacy_rules(self):
        manager, api, _, _ = self.make_sync_manager(
            answers=["通知対象を同期する"],
            filters=[legacy_managed("legacy-a", ADDRESS_A), legacy_managed("legacy-b", ADDRESS_B)],
            expected=[ADDRESS_A, ADDRESS_B],
        )
        self.assertTrue(manager.sync_targets())
        add_indexes = [i for i, event in enumerate(api.events) if event[0] == "add"]
        delete_indexes = [i for i, event in enumerate(api.events) if event[0] == "delete"]
        self.assertEqual(2, len(add_indexes))
        self.assertEqual(2, len(delete_indexes))
        self.assertLess(max(add_indexes), min(delete_indexes))

    def test_target_sync_resumes_legacy_retirement_after_process_crash(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([legacy_managed("legacy-a", ADDRESS_A), legacy_managed("legacy-b", ADDRESS_B)])
        api.discovered = [ADDRESS_A, ADDRESS_B]
        original_delete = api.delete_filter
        calls = 0
        def delete_then_crash(filter_id):
            nonlocal calls
            original_delete(filter_id)
            calls += 1
            if calls == 1:
                raise TimeoutError("process died")
        api.delete_filter = delete_then_crash
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        config = {"notification_base_address": ADDRESS_A,
                  "notification_targets": [ADDRESS_A, ADDRESS_B]}
        first = MailManager(api, FakeDeployer(config), "/private/mail-forward-command",
                            input_fn=lambda _p="": "通知対象を同期する", output_fn=lambda _m: None,
                            config=config, release_workflow=Workflow())
        with self.assertRaises(TimeoutError):
            first.sync_targets()
        api.delete_filter = original_delete
        second = MailManager(api, FakeDeployer(config), "/private/mail-forward-command",
                             input_fn=lambda _p="": "通知対象を同期する", output_fn=lambda _m: None,
                             config=config, release_workflow=Workflow())
        self.assertTrue(second.sync_targets())
        self.assertFalse(any(second._legacy_managed(rule) for rule in api.filters))

    def test_target_sync_fresh_process_resumes_interrupted_add_from_ftps_journal(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([managed("base", ADDRESS_A)])
        api.discovered = [ADDRESS_A, ADDRESS_B]
        original_add = api.add_filter
        def add_then_interrupt(rule):
            original_add(rule)
            raise TimeoutError("connection lost")
        api.add_filter = add_then_interrupt
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        config = {"notification_base_address": ADDRESS_A,
                  "notification_targets": [ADDRESS_A]}
        first = MailManager(api, FakeDeployer(config), "/private/mail-forward-command",
                            input_fn=lambda _p="": "通知対象を同期する",
                            output_fn=lambda _m: None, config=config,
                            release_workflow=Workflow())
        with self.assertRaises(TimeoutError):
            first.sync_targets()
        journal_path = Workflow.PRIVATE_ROOT + "/deploy-transactions/target-sync-scope.json"
        self.assertIn(journal_path, shared_ftps.files)
        self.assertIn(("journal-cas", "target-sync"), shared_ftps.calls)
        api.add_filter = original_add
        second_deployer = FakeDeployer(config)
        second = MailManager(api, second_deployer, "/private/mail-forward-command",
                             input_fn=lambda _p="": "通知対象を同期する",
                             output_fn=lambda _m: None, config=config,
                             release_workflow=Workflow())
        self.assertTrue(second.sync_targets())
        self.assertEqual([ADDRESS_A, ADDRESS_B], second_deployer.config["notification_targets"])
        self.assertEqual(1, len(api.added))

    def test_target_sync_fresh_process_resumes_interrupted_delete_from_ftps_journal(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([managed("base", ADDRESS_A), managed("stale", ADDRESS_C)])
        api.discovered = [ADDRESS_A]
        original_delete = api.delete_filter
        def delete_then_interrupt(rule_id):
            original_delete(rule_id)
            raise TimeoutError("connection lost")
        api.delete_filter = delete_then_interrupt
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        config = {"notification_base_address": ADDRESS_A,
                  "notification_targets": [ADDRESS_A, ADDRESS_C]}
        first = MailManager(api, FakeDeployer(config), "/private/mail-forward-command",
                            input_fn=lambda _p="": "通知対象を同期する",
                            output_fn=lambda _m: None, config=config,
                            release_workflow=Workflow())
        with self.assertRaises(TimeoutError):
            first.sync_targets()
        journal_path = Workflow.PRIVATE_ROOT + "/deploy-transactions/target-sync-scope.json"
        self.assertIn(journal_path, shared_ftps.files)
        self.assertIn(("journal-cas", "target-sync"), shared_ftps.calls)
        api.delete_filter = original_delete
        second_deployer = FakeDeployer(config)
        second = MailManager(api, second_deployer, "/private/mail-forward-command",
                             input_fn=lambda _p="": "通知対象を同期する",
                             output_fn=lambda _m: None, config=config,
                             release_workflow=Workflow())
        self.assertTrue(second.sync_targets())
        self.assertEqual([ADDRESS_A], second_deployer.config["notification_targets"])
        self.assertEqual(["stale"], api.deleted)

    def test_target_sync_restart_adopts_exact_post_cas_config_without_extra_mutation(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([managed("base", ADDRESS_A)])
        api.discovered = [ADDRESS_A, ADDRESS_B]
        old_config = {"notification_base_address": ADDRESS_A,
                      "notification_targets": [ADDRESS_A]}
        first_deployer = FakeDeployer(old_config)
        original_update = first_deployer.update_private_config
        def upload_then_interrupt(config):
            original_update(config)
            raise TimeoutError("process died after upload")
        first_deployer.update_private_config = upload_then_interrupt
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        first = MailManager(api, first_deployer, "/private/mail-forward-command",
                            input_fn=lambda _p="": "通知対象を同期する",
                            output_fn=lambda _m: None, config=old_config,
                            release_workflow=Workflow())
        with self.assertRaises(TimeoutError): first.sync_targets()
        updated = dict(first_deployer.config)
        second_deployer = FakeDeployer(updated)
        mutations_before = (len(api.added), len(api.deleted))
        second = MailManager(api, second_deployer, "/private/mail-forward-command",
                             input_fn=lambda _p="": "通知対象を同期する",
                             output_fn=lambda _m: None, config=updated,
                             release_workflow=Workflow())
        self.assertTrue(second.sync_targets())
        journal_path = Workflow.PRIVATE_ROOT + "/deploy-transactions/target-sync-scope.json"
        journal = json.loads(shared_ftps.files[journal_path])
        self.assertEqual("committed", journal["phase"])
        self.assertEqual(FakeDeployer._digest(updated), journal["config_applied_sha256"])
        self.assertEqual([], second_deployer.configs)
        self.assertEqual(mutations_before, (len(api.added), len(api.deleted)))

    def test_target_sync_restart_commits_after_config_readback_then_process_died(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([managed("base", ADDRESS_A)])
        api.discovered = [ADDRESS_A, ADDRESS_B]
        old_config = {"notification_base_address": ADDRESS_A,
                      "notification_targets": [ADDRESS_A]}
        first_deployer = FakeDeployer(old_config)
        original_replace = shared_ftps.compare_and_swap_scope_journal
        def interrupt_before_commit(kind, expected, body):
            if json.loads(body)["phase"] == "committed":
                raise TimeoutError("process died before journal commit")
            return original_replace(kind, expected, body)
        shared_ftps.compare_and_swap_scope_journal = interrupt_before_commit
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        first = MailManager(api, first_deployer, "/private/mail-forward-command",
                            input_fn=lambda _p="": "通知対象を同期する",
                            output_fn=lambda _m: None, config=old_config,
                            release_workflow=Workflow())
        with self.assertRaises(RuntimeError): first.sync_targets()
        shared_ftps.compare_and_swap_scope_journal = original_replace
        updated = dict(first_deployer.config)
        second = MailManager(api, FakeDeployer(updated), "/private/mail-forward-command",
                             input_fn=lambda _p="": "通知対象を同期する",
                             output_fn=lambda _m: None, config=updated,
                             release_workflow=Workflow())
        self.assertTrue(second.sync_targets())
        journal_path = Workflow.PRIVATE_ROOT + "/deploy-transactions/target-sync-scope.json"
        self.assertEqual("committed", json.loads(shared_ftps.files[journal_path])["phase"])
        self.assertEqual(1, len(api.added))

    def test_target_sync_completed_recovery_rejects_injected_unrelated_api_id(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([managed("base", ADDRESS_A)])
        api.discovered = [ADDRESS_A, ADDRESS_B]
        old_config = {"notification_base_address": ADDRESS_A,
                      "notification_targets": [ADDRESS_A]}
        first_deployer = FakeDeployer(old_config)
        original_update = first_deployer.update_private_config
        def upload_then_interrupt(config):
            original_update(config)
            raise TimeoutError("process died after upload")
        first_deployer.update_private_config = upload_then_interrupt
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        first = MailManager(api, first_deployer, "/private/mail-forward-command",
                            input_fn=lambda _p="": "通知対象を同期する",
                            output_fn=lambda _m: None, config=old_config,
                            release_workflow=Workflow())
        with self.assertRaises(TimeoutError): first.sync_targets()
        api.filters.append({"id": "injected", "domain": "example.invalid",
                            "opaque": "not-authorized-by-journal"})
        mutations_before = len(api.added) + len(api.deleted)
        updated = dict(first_deployer.config)
        second = MailManager(api, FakeDeployer(updated), "/private/mail-forward-command",
                             input_fn=lambda _p="": "通知対象を同期する",
                             output_fn=lambda _m: None, config=updated,
                             release_workflow=Workflow())
        with self.assertRaisesRegex(RuntimeError, "baseline"):
            second.sync_targets()
        journal_path = Workflow.PRIVATE_ROOT + "/deploy-transactions/target-sync-scope.json"
        self.assertEqual("active", json.loads(shared_ftps.files[journal_path])["phase"])
        self.assertEqual(mutations_before, len(api.added) + len(api.deleted))

    def test_notification_pinned_target_menu_exposes_four_actions(self):
        self.assertIn("1. 恒久通知対象一覧", MailManager.MENU)
        self.assertIn("2. 恒久通知対象追加（複数可）", MailManager.MENU)
        self.assertIn("3. 恒久通知対象変更", MailManager.MENU)
        self.assertIn("4. 恒久通知対象削除", MailManager.MENU)

    def test_pinned_target_list_add_change_delete_and_auto_overlap(self):
        key = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"
        config = {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A, ADDRESS_B],
            "notification_pinned_targets": [ADDRESS_B],
            "system_mail_hmac_key": key,
            "command_path": "/private/mail-forward-command",
        }
        manager, api, deployer, output = self.make_sync_manager(
            answers=[
                ADDRESS_C, "通知対象を同期する",
                ADDRESS_B, ADDRESS_A, "通知対象を同期する",
                ADDRESS_C, "通知対象を同期する",
            ], filters=[managed("auto", ADDRESS_A), managed("pinned", ADDRESS_B)],
            expected=[ADDRESS_A], config=config,
        )
        manager.list_targets()
        self.assertTrue(manager.add_targets())
        self.assertEqual([ADDRESS_B, ADDRESS_C],
                         deployer.configs[-1]["notification_pinned_targets"])
        self.assertEqual([ADDRESS_A, ADDRESS_B, ADDRESS_C],
                         deployer.configs[-1]["notification_targets"])
        self.assertTrue(manager.change_target())
        self.assertEqual([ADDRESS_A, ADDRESS_C],
                         deployer.configs[-1]["notification_pinned_targets"])
        self.assertEqual([ADDRESS_A, ADDRESS_C],
                         deployer.configs[-1]["notification_targets"])
        self.assertTrue(manager.delete_target())
        self.assertEqual([ADDRESS_A],
                         deployer.configs[-1]["notification_pinned_targets"])
        self.assertEqual([ADDRESS_A], deployer.configs[-1]["notification_targets"])
        self.assertEqual(key, deployer.configs[-1]["system_mail_hmac_key"])
        self.assertIn("恒久通知対象: " + ADDRESS_B, output)
        self.assertEqual(1, sum(1 for rule in api.filters
                                if manager._address(rule) == ADDRESS_A))

    def test_pinned_target_refuses_to_delete_auto_only_and_japanese_cancellation(self):
        config = {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A],
            "notification_pinned_targets": [],
            "command_path": "/private/mail-forward-command",
        }
        manager, api, deployer, output = self.make_sync_manager(
            answers=[ADDRESS_A], filters=[managed("auto", ADDRESS_A)],
            expected=[ADDRESS_A], config=config,
        )
        with self.assertRaisesRegex(ValueError, "自動検出"):
            manager.delete_target()
        self.assertEqual([], deployer.configs)
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))
        cancelled, api2, deployer2, _ = self.make_sync_manager(
            answers=[ADDRESS_B, "キャンセル"], filters=[managed("auto", ADDRESS_A)],
            expected=[ADDRESS_A], config=config,
        )
        self.assertFalse(cancelled.add_targets())
        self.assertEqual([], deployer2.configs)
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api2.events))

    def test_fresh_manager_resumes_pinned_journal_intent_with_old_config(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([managed("auto", ADDRESS_A)])
        api.discovered = [ADDRESS_A]
        original_add = api.add_filter
        def add_then_interrupt(rule):
            original_add(rule)
            raise TimeoutError("connection lost")
        api.add_filter = add_then_interrupt
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        config = {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A],
            "notification_pinned_targets": [],
            "command_path": "/private/mail-forward-command",
        }
        first_answers = iter([ADDRESS_B, "通知対象を同期する"])
        first = MailManager(
            api, FakeDeployer(config), "/private/mail-forward-command",
            input_fn=lambda _prompt="": next(first_answers),
            output_fn=lambda _message: None, config=config, release_workflow=Workflow(),
        )
        with self.assertRaises(TimeoutError):
            first.add_targets()
        api.add_filter = original_add
        second_deployer = FakeDeployer(config)
        second = MailManager(
            api, second_deployer, "/private/mail-forward-command",
            input_fn=lambda _prompt="": "通知対象を同期する",
            output_fn=lambda _message: None, config=config, release_workflow=Workflow(),
        )
        self.assertTrue(second.sync_targets())
        self.assertEqual([ADDRESS_B],
                         second_deployer.config["notification_pinned_targets"])
        self.assertEqual([ADDRESS_A, ADDRESS_B],
                         second_deployer.config["notification_targets"])

    def test_pinned_target_bounds_ignore_notification_targets_when_error_recipients_fit(self):
        key = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"
        recipients = [f"r{index:02d}@example.invalid" for index in range(32)]
        config = {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A],
            "notification_pinned_targets": [],
            "error_recipients": recipients,
            "system_mail_hmac_key": key,
            "log_path": "/home/example/private/notifier.jsonl",
            "command_path": "/private/mail-forward-command",
        }
        manager, api, deployer, _ = self.make_sync_manager(
            answers=[ADDRESS_B, "通知対象を同期する"], filters=[managed("auto", ADDRESS_A)],
            expected=[ADDRESS_A], config=config,
        )
        self.assertTrue(manager.add_targets())
        self.assertEqual([ADDRESS_A, ADDRESS_B], deployer.configs[-1]["notification_targets"])
        self.assertTrue(any(event[0] == "add" for event in api.events))

    def test_noncommitted_v1_target_journal_stops_with_zero_mutations(self):
        shared_ftps = MigrationFtps()
        path = "/private/xserver-mail-lineworks/deploy-transactions/target-sync-scope.json"
        old_rule = managed("old", ADDRESS_A)
        ScopeJournal(shared_ftps, path).prepare([old_rule], [old_rule], [])
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        config = {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A],
            "notification_pinned_targets": [],
        }
        api = FakeApi([old_rule])
        deployer = FakeDeployer(config)
        manager = MailManager(
            api, deployer, "/private/mail-forward-command",
            input_fn=lambda _prompt="": "通知対象を同期する",
            output_fn=lambda _message: None, config=config, release_workflow=Workflow(),
        )
        with self.assertRaisesRegex(RuntimeError, "旧形式"):
            manager.sync_targets()
        self.assertEqual([], deployer.configs)
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))

    def _make_v1_closure_manager(self, *, state_change=None, filters_change=None,
                                 config_change=None, discovered=None, answer=None):
        shared_ftps = MigrationFtps()
        path = "/private/xserver-mail-lineworks/deploy-transactions/target-sync-scope.json"
        old_rule = legacy_managed("old-1", ADDRESS_A)
        new_rule = managed("new-1", ADDRESS_A)
        unrelated = {"id": "keep-1", "domain": "example.invalid", "opaque": 1}
        old_hash = ScopeJournal.digest(ScopeJournal.body(old_rule))
        new_hash = ScopeJournal.digest(ScopeJournal.body(new_rule))
        state = {
            "schema_version": 1, "phase": "active",
            "old": [{"id": "old-1", "sha256": old_hash}],
            "new": [new_hash],
            "unrelated": [{"id": "keep-1", "sha256": ScopeJournal.digest(unrelated)}],
            "pending": {"kind": "delete", "id": "old-1", "sha256": old_hash},
            "new_ids": [{"id": "new-1", "sha256": new_hash}],
            "retired_ids": [],
        }
        if state_change is not None:
            state_change(state, old_rule, new_rule, unrelated)
        journal = ScopeJournal(shared_ftps, path)
        _missing, raw = journal.read_exact()
        journal.write(state, expected=raw)
        shared_ftps.calls.clear()

        class Deployment:
            ftps = shared_ftps

        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()

        config = {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A],
            "notification_pinned_targets": [],
            "unknown": {"preserved": True},
        }
        if config_change is not None:
            config_change(config)
        filters = [new_rule, unrelated, managed("retire-1", ADDRESS_C)]
        if filters_change is not None:
            filters_change(filters, old_rule, new_rule, unrelated)
        api = FakeApi(filters)
        api.discovered = list([ADDRESS_A] if discovered is None else discovered)
        deployer = FakeDeployer(config)
        output = []
        answer_fn = answer or (lambda _prompt="": "旧同期journalを完了として閉じる")
        manager = MailManager(
            api, deployer, "/private/mail-forward-command",
            input_fn=answer_fn,
            output_fn=output.append, config=config, release_workflow=Workflow(),
        )
        return manager, api, deployer, shared_ftps, path, state, output

    def test_menu_sync_closes_ambiguously_completed_v1_delete_journal_only(self):
        manager, api, deployer, shared_ftps, path, state, output = \
            self._make_v1_closure_manager()

        self.assertTrue(manager.sync_targets())
        closed = json.loads(shared_ftps.files[path])
        self.assertEqual("committed", closed["phase"])
        self.assertIsNone(closed["pending"])
        self.assertEqual(["old-1"], closed["retired_ids"])
        self.assertEqual([], deployer.configs)
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))
        self.assertIn("journal外の現行filter: 1件", "\n".join(output))
        writes = [call for call in shared_ftps.calls if call[0] == "journal-cas"]
        self.assertEqual(1, len(writes))
        self.assertEqual(1, closed["schema_version"])

    def _assert_v1_closure_has_zero_mutations(self, fixture):
        _manager, api, deployer, shared_ftps, _path, _state, _output = fixture
        self.assertEqual([], deployer.configs)
        self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))
        self.assertFalse(any(call[0] == "journal-cas" for call in shared_ftps.calls))

    def test_v1_closure_is_menu_sync_only_and_rejects_other_pending_shapes(self):
        fixture = self._make_v1_closure_manager()
        with self.assertRaisesRegex(RuntimeError, "旧形式"):
            fixture[0].sync_targets([ADDRESS_A])
        self._assert_v1_closure_has_zero_mutations(fixture)

        def pending_add(state, _old, _new, _unrelated):
            state["pending"] = {
                "kind": "add", "id": "new-1", "sha256": state["new"][0],
            }
        fixture = self._make_v1_closure_manager(state_change=pending_add)
        with self.assertRaisesRegex(RuntimeError, "旧形式"):
            fixture[0].sync_targets()
        self._assert_v1_closure_has_zero_mutations(fixture)

        for label in ("pending-already-retired", "new-id-retired"):
            with self.subTest(label=label):
                def inconsistent_retired(state, _old, _new, _unrelated,
                                         selected=label):
                    state["retired_ids"] = {
                        "pending-already-retired": ["old-1"],
                        "new-id-retired": ["new-1"],
                    }[selected]
                fixture = self._make_v1_closure_manager(
                    state_change=inconsistent_retired)
                with self.assertRaises(RuntimeError):
                    fixture[0].sync_targets()
                self._assert_v1_closure_has_zero_mutations(fixture)

        fixture = self._make_v1_closure_manager()
        raw_state = json.loads(fixture[3].files[fixture[4]])
        raw_state["retired_ids"] = ["unknown-1"]
        fixture[3].files[fixture[4]] = (json.dumps(
            raw_state, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self.assertRaises(RuntimeError):
            fixture[0].sync_targets()
        self._assert_v1_closure_has_zero_mutations(fixture)

        def pending_new_delete(state, _old, _new, _unrelated):
            state["pending"] = {
                "kind": "delete", "id": "new-1", "sha256": state["new"][0],
            }
        fixture = self._make_v1_closure_manager(state_change=pending_new_delete)
        with self.assertRaisesRegex(RuntimeError, "旧形式"):
            fixture[0].sync_targets()
        self._assert_v1_closure_has_zero_mutations(fixture)

    def test_v1_closure_rejects_api_identity_body_and_hash_mismatches(self):
        cases = {
            "old-present": lambda filters, old, _new, _unrelated: filters.append(old),
            "new-missing": lambda filters, _old, _new, _unrelated: filters.__setitem__(
                slice(None), [item for item in filters if item.get("id") != "new-1"]),
            "new-body": lambda filters, _old, _new, _unrelated: next(
                item for item in filters if item.get("id") == "new-1"
            )["action"].__setitem__("target", "changed"),
            "unrelated-body": lambda filters, _old, _new, _unrelated: next(
                item for item in filters if item.get("id") == "keep-1"
            ).__setitem__("opaque", 2),
            "duplicate-id": lambda filters, _old, _new, _unrelated: filters.append(
                {"id": "new-1", "opaque": 3}),
            "arbitrary-unknown": lambda filters, _old, _new, _unrelated: filters.append(
                {"id": "arbitrary-1", "opaque": 4}),
        }
        for label, change in cases.items():
            with self.subTest(label=label):
                fixture = self._make_v1_closure_manager(filters_change=change)
                with self.assertRaises(RuntimeError):
                    fixture[0].sync_targets()
                self._assert_v1_closure_has_zero_mutations(fixture)

        def journal_new_hash_mismatch(state, _old, _new, _unrelated):
            state["new"] = ["f" * 64]
            state["new_ids"][0]["sha256"] = "f" * 64
        fixture = self._make_v1_closure_manager(state_change=journal_new_hash_mismatch)
        with self.assertRaises(RuntimeError):
            fixture[0].sync_targets()
        self._assert_v1_closure_has_zero_mutations(fixture)

    def test_v1_closure_rejects_config_automatic_and_rule_intent_mismatch(self):
        def extra_config_target(config):
            config["notification_targets"] = [ADDRESS_A, ADDRESS_B]
        fixture = self._make_v1_closure_manager(config_change=extra_config_target)
        with self.assertRaises(RuntimeError):
            fixture[0].sync_targets()
        self._assert_v1_closure_has_zero_mutations(fixture)

        def matching_extra_target(config):
            config["notification_targets"] = [ADDRESS_A, ADDRESS_B]
        fixture = self._make_v1_closure_manager(
            config_change=matching_extra_target, discovered=[ADDRESS_A, ADDRESS_B])
        with self.assertRaises(RuntimeError):
            fixture[0].sync_targets()
        self._assert_v1_closure_has_zero_mutations(fixture)

    def test_v1_closure_cancel_and_confirmation_readback_drifts_write_nothing(self):
        fixture = self._make_v1_closure_manager(answer=lambda _prompt="": "")
        self.assertFalse(fixture[0].sync_targets())
        self._assert_v1_closure_has_zero_mutations(fixture)

        for label in ("journal", "config", "api", "automatic"):
            with self.subTest(label=label):
                fixture_ref = {}
                def drift(_prompt="", selected=label):
                    manager, api, deployer, ftps, path, _state, _output = fixture_ref["value"]
                    if selected == "journal":
                        ftps.files[path] = ftps.files[path][:-1] + b" \n"
                    elif selected == "config":
                        deployer.config["unknown"] = {"preserved": False}
                    elif selected == "automatic":
                        api.discovered = [ADDRESS_A, ADDRESS_B]
                    else:
                        api.filters.append({"id": "late-unknown", "opaque": 4})
                    return "旧同期journalを完了として閉じる"
                fixture = self._make_v1_closure_manager(answer=drift)
                fixture_ref["value"] = fixture
                with self.assertRaises(RuntimeError):
                    fixture[0].sync_targets()
                self._assert_v1_closure_has_zero_mutations(fixture)

    def test_v1_closure_warns_only_with_counts_for_unknown_and_missing_unrelated(self):
        def missing_unrelated(filters, _old, _new, _unrelated):
            filters[:] = [item for item in filters if item.get("id") != "keep-1"]
        fixture = self._make_v1_closure_manager(filters_change=missing_unrelated)
        self.assertTrue(fixture[0].sync_targets())
        rendered = "\n".join(fixture[-1])
        self.assertIn("journal外の現行filter: 1件", rendered)
        self.assertIn("履歴上の無関係filter欠損: 1件", rendered)
        for secret in ("retire-1", "keep-1", ADDRESS_A, ADDRESS_C):
            self.assertNotIn(secret, rendered)

    def test_v1_closure_write_failure_and_readback_mismatch_stop_without_other_mutation(self):
        for label in ("failure", "mismatch"):
            with self.subTest(label=label):
                fixture = self._make_v1_closure_manager()
                ftps = fixture[3]
                original = ftps.compare_and_swap_scope_journal
                if label == "failure":
                    ftps.compare_and_swap_scope_journal = lambda _kind, _expected, _body: (_ for _ in ()).throw(
                        OSError("write failed"))
                else:
                    ftps.compare_and_swap_scope_journal = lambda kind, expected, body: \
                        original(kind, expected, body) + b" "
                with self.assertRaises(RuntimeError):
                    fixture[0].sync_targets()
                self.assertEqual([], fixture[2].configs)
                self.assertFalse(any(event[0] in {"add", "delete"}
                                     for event in fixture[1].events))

    def test_v1_closure_post_write_drift_never_adds_a_second_mutation(self):
        for label in ("config", "api", "automatic"):
            with self.subTest(label=label):
                fixture = self._make_v1_closure_manager()
                manager, api, deployer, ftps, _path, _state, _output = fixture
                original = ftps.compare_and_swap_scope_journal
                def write_then_drift(kind, expected, body, selected=label):
                    result = original(kind, expected, body)
                    if selected == "config":
                        deployer.config["unknown"] = {"preserved": False}
                    elif selected == "automatic":
                        api.discovered = [ADDRESS_A, ADDRESS_B]
                    else:
                        api.filters.append({"id": "post-write", "opaque": 5})
                    return result
                ftps.compare_and_swap_scope_journal = write_then_drift
                with self.assertRaisesRegex(RuntimeError, "完了後|drift|変更"):
                    manager.sync_targets()
                writes = [call for call in ftps.calls if call[0] == "journal-cas"]
                self.assertEqual(1, len(writes))
                self.assertEqual([], deployer.configs)
                self.assertFalse(any(event[0] in {"add", "delete"} for event in api.events))

    def test_v1_closure_returns_before_v2_and_next_call_starts_fresh_v2(self):
        fixture = self._make_v1_closure_manager()
        manager, api, _deployer, ftps, path, _state, _output = fixture
        self.assertTrue(manager.sync_targets())
        first = json.loads(ftps.files[path])
        self.assertEqual(1, first["schema_version"])
        manager.input = lambda _prompt="": "通知対象を同期する"
        self.assertTrue(manager.sync_targets())
        second = json.loads(ftps.files[path])
        self.assertEqual(2, second["schema_version"])
        self.assertEqual("committed", second["phase"])
        self.assertTrue(any(event[0] == "delete" for event in api.events))

    def test_final_readback_rejects_unrelated_rule_change_before_journal_commit(self):
        for mutation in ("add", "remove", "change"):
            with self.subTest(mutation=mutation):
                shared_ftps = MigrationFtps()
                unrelated = {"id": "keep", "domain": "example.invalid", "opaque": 1}
                api = FakeApi([managed("auto", ADDRESS_A), unrelated])
                api.discovered = [ADDRESS_A]
                class Deployment: ftps = shared_ftps
                class Workflow:
                    PRIVATE_ROOT = "/private/xserver-mail-lineworks"
                    deployer = Deployment()
                config = {
                    "notification_base_address": ADDRESS_A,
                    "notification_targets": [ADDRESS_A],
                    "notification_pinned_targets": [],
                    "unknown": "preserve",
                }
                deployer = FakeDeployer(config)
                original_read = deployer.read_private_config
                reads = 0
                def read_then_mutate_api():
                    nonlocal reads
                    reads += 1
                    value = original_read()
                    if reads == 6:
                        if mutation == "add":
                            api.filters.append({"id": "injected", "opaque": 2})
                        elif mutation == "remove":
                            api.filters = [rule for rule in api.filters
                                           if rule.get("id") != "keep"]
                        else:
                            next(rule for rule in api.filters
                                 if rule.get("id") == "keep")["opaque"] = 2
                    return value
                deployer.read_private_config = read_then_mutate_api
                answers = iter([ADDRESS_B, "通知対象を同期する"])
                manager = MailManager(
                    api, deployer, "/private/mail-forward-command",
                    input_fn=lambda _prompt="": next(answers),
                    output_fn=lambda _message: None, config=config,
                    release_workflow=Workflow(),
                )
                with self.assertRaisesRegex(RuntimeError, "最終|baseline"):
                    manager.add_targets()
                journal_path = (Workflow.PRIVATE_ROOT
                                + "/deploy-transactions/target-sync-scope.json")
                self.assertNotEqual(
                    "committed", json.loads(shared_ftps.files[journal_path])["phase"])

    def _interrupted_pinned_transaction_before_config_cas(self):
        shared_ftps = MigrationFtps()
        api = FakeApi([managed("auto", ADDRESS_A)])
        api.discovered = [ADDRESS_A]
        class Deployment: ftps = shared_ftps
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
        config = {
            "notification_base_address": ADDRESS_A,
            "notification_targets": [ADDRESS_A],
            "notification_pinned_targets": [],
            "unknown": 1,
        }
        deployer = FakeDeployer(config)
        original_read = deployer.read_private_config
        reads = 0
        def interrupt_before_cas():
            nonlocal reads
            reads += 1
            if reads == 3:
                raise OSError("interrupted before config CAS")
            return original_read()
        deployer.read_private_config = interrupt_before_cas
        answers = iter([ADDRESS_B, "通知対象を同期する"])
        first = MailManager(
            api, deployer, "/private/mail-forward-command",
            input_fn=lambda _prompt="": next(answers),
            output_fn=lambda _message: None, config=config,
            release_workflow=Workflow(),
        )
        with self.assertRaises(RuntimeError):
            first.add_targets()
        deployer.read_private_config = original_read
        return shared_ftps, api, deployer, Workflow, config

    def test_recovery_rejects_partial_intended_arrays(self):
        shared_ftps, api, deployer, workflow, config = \
            self._interrupted_pinned_transaction_before_config_cas()
        external = dict(config, notification_pinned_targets=[ADDRESS_B])
        deployer.config = dict(external)
        second = MailManager(
            api, deployer, "/private/mail-forward-command",
            input_fn=lambda _prompt="": "通知対象を同期する",
            output_fn=lambda _message: None, config=external,
            release_workflow=workflow(),
        )
        with self.assertRaisesRegex(RuntimeError, "CAS|証跡|変更"):
            second.sync_targets()
        path = workflow.PRIVATE_ROOT + "/deploy-transactions/target-sync-scope.json"
        self.assertNotEqual("committed", json.loads(shared_ftps.files[path])["phase"])

    def test_recovery_rejects_concurrent_unrelated_config_change(self):
        shared_ftps, api, deployer, workflow, config = \
            self._interrupted_pinned_transaction_before_config_cas()
        external = dict(config, notification_pinned_targets=[ADDRESS_B],
                        notification_targets=[ADDRESS_A, ADDRESS_B], unknown=2)
        deployer.config = dict(external)
        second = MailManager(
            api, deployer, "/private/mail-forward-command",
            input_fn=lambda _prompt="": "通知対象を同期する",
            output_fn=lambda _message: None, config=external,
            release_workflow=workflow(),
        )
        with self.assertRaisesRegex(RuntimeError, "CAS|証跡|変更"):
            second.sync_targets()
        path = workflow.PRIVATE_ROOT + "/deploy-transactions/target-sync-scope.json"
        self.assertNotEqual("committed", json.loads(shared_ftps.files[path])["phase"])

    def test_filesystem_config_path_combines_two_validated_private_absolute_paths(self):
        self.assertEqual("/home/example/mail-lineworks/private/config.json",
                         _filesystem_config_path("/home/example", "/mail-lineworks/private/config.json"))
        for home, config in (("home/example", "/private/config.json"),
                             ("/home/example", "private/config.json"),
                             ("/home/public_html", "/private/config.json"),
                             ("/home/example", "/private/../config.json")):
            with self.subTest(home=home, config=config), self.assertRaises(ValueError):
                _filesystem_config_path(home, config)

    def test_bootstrap_asset_resolver_has_exact_repo_and_bundle_layouts(self):
        bundle_manager = Path(
            "/Applications/Xserver.app/Contents/Resources/manager/manage.py")
        fixed = bundle_manager.parent.parent / "fixed-runtime"
        self.assertEqual((fixed / "manage-private-config.php",
                          fixed / "legacy-manifest.json", 0o600),
                         _legacy_bootstrap_asset_paths(bundle_manager))
        repository = Path(__file__).resolve().parents[2]
        self.assertEqual((repository / "bin/manage-private-config.php",
                          repository / "fixed-runtime/legacy-manifest.json", 0o644),
                         _legacy_bootstrap_asset_paths(repository / "manager/manage.py"))

    MAIN_ENVIRONMENT = {
        "XSERVER_SERVERNAME": "server.example.invalid",
        "XSERVER_COMMAND_PATH": "/mail-lineworks/current/bin/mail-to-lineworks.php",
        "XSERVER_FTPS_HOST": "ftp.example.invalid",
        "XSERVER_CONFIG_PATH": "/mail-lineworks/private/config.json",
        "XSERVER_HOME": "/home/example",
        "XSERVER_SSH_ALIAS": "safe-alias",
    }

    def make_manager(self, answers=(), filters=(), errors=(), config=None, read_error=None,
                     now_fn=None, test_token_fn=None, read_configs=None,
                     initial_command_path=None):
        output = []
        api = FakeApi(filters)
        if config is None and errors:
            config = {"error_recipients": list(errors)}
        deployer = FakeDeployer(config, read_error, read_configs)
        answers = iter(answers)
        manager = MailManager(
            api,
            deployer,
            "/private/mail-forward-command",
            input_fn=lambda prompt="": next(answers),
            output_fn=output.append,
            error_recipients=list(errors), config=config,
            now_fn=now_fn, test_token_fn=test_token_fn,
            initial_command_path=initial_command_path,
            private_config_client=deployer,
        )
        return manager, api, deployer, output

    def test_email_validation_uses_parser_and_rejects_newlines(self):
        self.assertEqual(ADDRESS_A, validate_email(ADDRESS_A))
        for invalid in ("alpha", "@example.invalid", "alpha@", "a@@example.invalid", "a@example.invalid\nBcc:x@example.invalid"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_email(invalid)

    def test_email_validation_rejects_invalid_local_and_domain_rules(self):
        invalid = (
            ".alpha@example.invalid", "alpha.@example.invalid", "a..b@example.invalid",
            "alpha@-example.invalid", "alpha@example-.invalid", "alpha@example_invalid",
            "alpha@example", "alpha@example..invalid", "alpha @example.invalid",
        )
        for address in invalid:
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_email(address)

    def test_lists_only_configured_pinned_notification_targets(self):
        unrelated = {"id": "other", "domain": "example.invalid", "managed": False}
        manager, _, _, output = self.make_manager(
            filters=[managed("one", ADDRESS_A), unrelated],
            config={"notification_pinned_targets": [ADDRESS_A]},
        )
        manager.list_targets()
        self.assertIn(ADDRESS_A, "\n".join(output))
        self.assertNotIn("other", "\n".join(output))

    def test_error_recipients_support_list_multiple_add_change_and_delete_with_deploy_confirmation(self):
        manager, _, deployer, output = self.make_manager(
            answers=[
                ADDRESS_B + "," + ADDRESS_C, "配備する",
                ADDRESS_A, ADDRESS_C, "変更して配備する",
                ADDRESS_B, "削除して配備する",
            ],
            errors=[ADDRESS_A],
        )
        manager.list_error_recipients()
        manager.add_error_recipients()
        manager.change_error_recipient()
        manager.delete_error_recipient()
        self.assertEqual([ADDRESS_C], deployer.configs[-1]["error_recipients"])
        self.assertIn("- " + ADDRESS_A, output)
        self.assertIn("+ " + ADDRESS_C, output)

    def test_cancelled_error_recipient_deploy_does_not_mutate_or_upload(self):
        manager, _, deployer, _ = self.make_manager(answers=[ADDRESS_B, "はい"], errors=[ADDRESS_A])
        manager.add_error_recipients()
        self.assertEqual([ADDRESS_A], manager.error_recipients)
        self.assertEqual([], deployer.configs)

    def test_last_error_recipient_cannot_be_deleted(self):
        manager, _, deployer, _ = self.make_manager(answers=[ADDRESS_A], errors=[ADDRESS_A])
        with self.assertRaisesRegex(ValueError, "1件以上"):
            manager.delete_error_recipient()
        self.assertEqual([], deployer.configs)

    def test_error_recipient_update_reads_and_merges_latest_remote_config(self):
        config = {"webhook_url": "secret-placeholder", "unknown": {"keep": True}, "error_recipients": [ADDRESS_A]}
        manager, _, deployer, _ = self.make_manager(answers=[ADDRESS_B, "配備する"], errors=[ADDRESS_A], config=config)
        manager.add_error_recipients()
        self.assertEqual("secret-placeholder", deployer.configs[-1]["webhook_url"])
        self.assertEqual({"keep": True}, deployer.configs[-1]["unknown"])

    def test_error_recipient_update_stops_when_remote_changed_since_startup(self):
        snapshot = {"unknown": 1, "error_recipients": [ADDRESS_A]}
        changed = {"unknown": 2, "error_recipients": [ADDRESS_C]}
        manager, _, deployer, output = self.make_manager(
            answers=[ADDRESS_B], errors=[ADDRESS_A], config=snapshot, read_configs=[changed]
        )
        self.assertFalse(manager.add_error_recipients())
        self.assertEqual([], deployer.configs)
        self.assertIn("競合", "\n".join(output))
        self.assertIn("+ " + ADDRESS_B, output)
        self.assertNotIn("- " + ADDRESS_A, output)

    def test_error_recipient_update_stops_when_remote_changes_after_confirmation(self):
        snapshot = {"unknown": 1, "error_recipients": [ADDRESS_A]}
        raced = {"unknown": 2, "error_recipients": [ADDRESS_A]}
        manager, _, deployer, output = self.make_manager(
            answers=[ADDRESS_B, "配備する"], errors=[ADDRESS_A], config=snapshot,
            read_configs=[snapshot, raced],
        )
        self.assertFalse(manager.add_error_recipients())
        self.assertEqual([], deployer.configs)
        self.assertIn("競合", "\n".join(output))

    def test_unreadable_config_never_uploads(self):
        manager, _, deployer, _ = self.make_manager(
            answers=[ADDRESS_B, "配備する"], errors=[ADDRESS_A], read_error=RuntimeError("invalid remote JSON")
        )
        with self.assertRaises(RuntimeError):
            manager.add_error_recipients()
        self.assertEqual([], deployer.configs)

    def test_default_diagnostics_compare_api_rules_with_remote_config_and_release(self):
        config = {
            "notification_targets": [ADDRESS_A], "notification_pinned_targets": [ADDRESS_A],
            "release_path": "/private/releases/r1",
            "command_path": "/private/mail-forward-command", "error_recipients": [],
        }
        manager, _, _, _ = self.make_manager(filters=[managed("one", ADDRESS_A)], config=config)
        manager.private_config_client.health_summary = lambda: {
            "state": "healthy", "changed_at": "2026-07-13T12:00:00Z",
            "classification": "success", "next_observation_sequence": 4,
            "last_applied_sequence": 4,
        }
        result = manager._default_diagnostics()
        self.assertIn("一致", result["sync"])
        self.assertEqual("一致（API登録 1/1件）", result["pinned"])
        self.assertEqual("一致（API 1件 / リモート設定 1件）", result["targets"])
        self.assertEqual("正常", result["health"])
        self.assertEqual("リモートリリース: 設定済み", result["latest_log"])
        self.assertNotIn("/private/releases/r1", "\n".join(result.values()))

    def test_diagnostics_report_pinned_and_target_drift_without_private_values(self):
        message_hash = "f" * 64
        config = {
            "notification_targets": [ADDRESS_A, ADDRESS_B],
            "notification_pinned_targets": [ADDRESS_B],
            "notification_base_address": ADDRESS_A,
            "command_path": "/private/mail-forward-command",
            "release_path": "/private/releases/private-release-id",
            "system_mail_hmac_key": "private-key-must-not-appear",
            "webhook_url": "https://webhook.worksmobile.com/message/private-token",
        }
        manager, _, _, output = self.make_manager(
            filters=[managed("one", ADDRESS_A)], config=config,
        )
        manager.private_config_client.health_summary = lambda: (_ for _ in ()).throw(
            RuntimeError(ADDRESS_B + " private-key-must-not-appear private-token " + message_hash)
        )

        manager.show_diagnostics()

        rendered = "\n".join(output)
        self.assertIn("恒久対象: 不一致（API登録 0/1件）", rendered)
        self.assertIn("通知対象: 不一致（API 1件 / リモート設定 2件）", rendered)
        self.assertIn("配信状態: 状態ファイル不正", rendered)
        for private in (ADDRESS_A, ADDRESS_B, "private-key-must-not-appear",
                        "private-token", message_hash,
                        "/private/releases/private-release-id"):
            self.assertNotIn(private, rendered)

    def test_diagnostics_report_missing_healthy_and_degraded_health_in_japanese(self):
        cases = (
            ({"state": "missing", "changed_at": None, "classification": None,
              "next_observation_sequence": 0, "last_applied_sequence": 0}, "未作成"),
            ({"state": "healthy", "changed_at": "2026-07-13T12:00:00Z",
              "classification": "success", "next_observation_sequence": 2,
              "last_applied_sequence": 2}, "正常"),
            ({"state": "degraded", "changed_at": "2026-07-13T12:00:00Z",
              "classification": "transport_error", "next_observation_sequence": 3,
              "last_applied_sequence": 3}, "障害中"),
        )
        for summary, expected in cases:
            with self.subTest(state=summary["state"]):
                config = {
                    "notification_targets": [ADDRESS_A],
                    "notification_pinned_targets": [],
                    "command_path": "/private/mail-forward-command",
                }
                manager, _, _, output = self.make_manager(
                    filters=[managed("one", ADDRESS_A)], config=config,
                )
                manager.private_config_client.health_summary = lambda summary=summary: dict(summary)
                manager.show_diagnostics()
                self.assertIn("配信状態: " + expected, output)

    def test_diagnostics_report_missing_target_or_command_metadata_as_out_of_sync(self):
        for config in ({"command_path": "/private/mail-forward-command"}, {"notification_targets": [ADDRESS_A]}):
            with self.subTest(config=config):
                manager, _, _, _ = self.make_manager(filters=[managed("one", ADDRESS_A)], config=config)
                self.assertIn("不一致", manager._default_diagnostics()["sync"])

    def test_diagnostics_treat_mixed_type_notification_targets_as_out_of_sync(self):
        config = {
            "notification_targets": [ADDRESS_A, 42],
            "command_path": "/private/mail-forward-command",
        }
        manager, _, _, _ = self.make_manager(filters=[managed("one", ADDRESS_A)], config=config)

        result = manager._default_diagnostics()

        self.assertIn("不一致", result["sync"])

    def test_lineworks_test_reads_remote_webhook_and_posts_https_json_without_outputting_secret(self):
        config = {"webhook_url": "https://webhook.worksmobile.com/message/secret-placeholder"}
        manager, _, _, output = self.make_manager(config=config)
        response = io.BytesIO(b"ok")
        response.status = 200
        context = object()
        with patch("manager.manage._build_verified_ssl_context", return_value=context), \
                patch("manager.manage.urlopen", return_value=response) as transport:
            manager.send_lineworks_test()
        request = transport.call_args.args[0]
        self.assertIs(context, transport.call_args.kwargs["context"])
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"title": "LINE WORKS接続テスト", "body": {"text": "管理CLIからのテスト通知です"}}, json.loads(request.data))
        self.assertNotIn("secret-placeholder", "\n".join(output))

    def test_lineworks_test_rejects_noncanonical_webhook_urls_without_network(self):
        invalid = (
            "http://webhook.worksmobile.com/message/token",
            "https://evil.example.invalid/message/token",
            "https://webhook.worksmobile.com:443/message/token",
            "https://user@webhook.worksmobile.com/message/token",
            "https://webhook.worksmobile.com/message/",
            "https://webhook.worksmobile.com/message/a/b",
            "https://webhook.worksmobile.com/message/token?x=1",
            "https://webhook.worksmobile.com/message/token#fragment",
        )
        for url in invalid:
            with self.subTest(url=url):
                manager, _, _, _ = self.make_manager(config={"webhook_url": url})
                with patch("manager.manage.urlopen") as transport, self.assertRaises(RuntimeError):
                    manager.send_lineworks_test()
                transport.assert_not_called()

    def test_error_mail_test_sets_expiring_subject_scoped_failure_after_exact_confirmation(self):
        token = "ERRTEST-" + "a" * 24
        config = {
            "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
            "error_recipients": [ADDRESS_A],
            "notification_targets": [],
            "notification_pinned_targets": [],
            "system_mail_hmac_key": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
            "log_path": "/home/example/private/notifier.jsonl",
            "unknown": 1,
        }
        manager, _, deployer, output = self.make_manager(
            answers=["エラーメールをテストする"], config=config,
            now_fn=lambda: datetime(2026, 7, 11, 1, 2, 3, tzinfo=timezone.utc),
            test_token_fn=lambda: token,
        )
        manager.send_error_mail_test()
        self.assertEqual("2026-07-11T01:12:03+00:00", deployer.configs[-1]["test_force_webhook_failure_until"])
        self.assertEqual(token, deployer.configs[-1]["test_error_subject_token"])
        self.assertNotIn("force_webhook_failure_once", deployer.configs[-1])
        self.assertEqual(1, deployer.configs[-1]["unknown"])
        rendered = "\n".join(output)
        self.assertIn("10分以内", rendered)
        self.assertIn(
            "準備完了: 10分以内に件名を「[Error Test %s]」に完全一致させた"
            "通知対象テストメールを1通送信し、エラー通知先への到着を確認してください" % token,
            output,
        )
        self.assertIn("完全一致", rendered)
        self.assertNotIn("含め", rendered)

    def test_error_mail_test_default_generator_is_runtime_compatible(self):
        manager, _, _, _ = self.make_manager()

        token = manager.test_token_fn()

        self.assertGreaterEqual(len(token), 32)
        self.assertLessEqual(len(token), 128)
        self.assertRegex(token, r"\AERRTEST-[A-Za-z0-9_-]+\Z")

    def test_error_mail_test_rejects_invalid_token_before_cas(self):
        config = {
            "webhook_url": "https://webhook.worksmobile.com/message/test-placeholder",
            "error_recipients": [ADDRESS_A],
            "notification_targets": [],
            "notification_pinned_targets": [],
            "system_mail_hmac_key": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
            "log_path": "/home/example/private/notifier.jsonl",
        }
        manager, _, deployer, _ = self.make_manager(
            answers=["エラーメールをテストする"], config=config,
            now_fn=lambda: datetime(2026, 7, 11, 1, 2, 3, tzinfo=timezone.utc),
            test_token_fn=lambda: "ERRTEST-too-short",
        )

        with self.assertRaisesRegex(RuntimeError, "テスト設定"):
            manager.send_error_mail_test()

        self.assertEqual([], deployer.configs)

    def test_runtime_config_rejects_unpaired_or_noncanonical_error_test_fields(self):
        base = {
            "error_recipients": [ADDRESS_A],
            "notification_targets": [],
            "notification_pinned_targets": [],
            "system_mail_hmac_key": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
            "log_path": "/home/example/private/notifier.jsonl",
        }
        token = "ERRTEST-" + "a" * 24
        invalid_fields = (
            {"test_error_subject_token": token},
            {"test_force_webhook_failure_until": "2026-07-11T01:12:03+00:00"},
            {"test_error_subject_token": None},
            {"test_force_webhook_failure_until": None},
            {"test_error_subject_token": None,
             "test_force_webhook_failure_until": "2026-07-11T01:12:03+00:00"},
            {"test_error_subject_token": token,
             "test_force_webhook_failure_until": None},
            {"test_error_subject_token": token,
             "test_force_webhook_failure_until": "2026-07-11T01:12:03Z"},
            {"test_error_subject_token": token,
             "test_force_webhook_failure_until": "2026-07-11T10:12:03+09:00"},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                candidate = {**base, **fields}
                client = FakePrivateConfigClient(candidate)
                manager = MailManager(
                    FakeApi(), FakeDeployer(), "/private/mail-forward-command",
                    config=candidate, private_config_client=client,
                    output_fn=lambda _message: None,
                )
                with self.assertRaisesRegex(RuntimeError, "テスト設定"):
                    manager.ensure_runtime_config()
                self.assertEqual([], client.cas_inputs)

    def test_runtime_config_accepts_inactive_null_error_test_fields(self):
        config = {
            "error_recipients": [ADDRESS_A],
            "notification_targets": [],
            "notification_pinned_targets": [],
            "system_mail_hmac_key": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
            "log_path": "/home/example/private/notifier.jsonl",
            "test_force_webhook_failure_until": None,
            "test_error_subject_token": None,
        }

        for strict in (False, True):
            with self.subTest(strict=strict):
                try:
                    validated = MailManager._validate_runtime_config(config, strict=strict)
                except RuntimeError:
                    self.fail("null/nullの通常無効テスト設定は受理する必要がある")
                self.assertIsNone(validated["test_force_webhook_failure_until"])
                self.assertIsNone(validated["test_error_subject_token"])

    def test_diagnostics_and_both_test_notifications_are_available(self):
        events = []
        manager, _, _, output = self.make_manager()
        manager.diagnostic_fn = lambda: {"sync": "ok", "latest_log": "redacted"}
        manager.lineworks_test_fn = lambda: events.append("lineworks")
        manager.error_mail_test_fn = lambda: events.append("error-mail")
        manager.show_diagnostics()
        manager.send_lineworks_test()
        manager.send_error_mail_test()
        self.assertEqual(["lineworks", "error-mail"], events)
        self.assertIn("同期状態: ok", output)
        self.assertIn("恒久対象: 不明", output)
        self.assertIn("通知対象: 不明", output)
        self.assertIn("配信状態: 不明", output)
        self.assertIn("最新ログ: redacted", output)

    def test_cancelled_error_test_callback_returns_false_without_success_message(self):
        manager, _, _, output = self.make_manager()
        manager.error_mail_test_fn = lambda: False
        self.assertFalse(manager.send_error_mail_test())
        self.assertNotIn("エラーメールテストを実行しました", output)

    def test_run_shows_japanese_menu_and_dispatches_until_exit(self):
        manager, _, _, output = self.make_manager(answers=["1", "0"])
        manager.run()
        rendered = "\n".join(output)
        self.assertIn("通知対象一覧", rendered)
        self.assertIn("同期診断", rendered)
        self.assertIn("LINE WORKSテスト", rendered)
        self.assertIn("エラーメールテスト", rendered)

    def test_release_action_stages_without_api_mutation_then_switches_after_second_confirmation(self):
        events = []
        class Workflow:
            def stage(self, source, release_id):
                events.append(("stage", source, release_id))
                return {"release_id": release_id}
            def switch(self, staged):
                events.append(("switch", staged["release_id"]))
        manager, api, _, output = self.make_manager(
            answers=["/tmp/release", "release-test", "検証配備する", "切替える"]
        )
        manager.release_workflow = Workflow()
        self.assertTrue(manager.deploy_release())
        self.assertEqual([
            ("stage", "/tmp/release", "release-test"), ("switch", "release-test")
        ], events)
        self.assertEqual([], api.added)
        self.assertEqual([], api.deleted)
        self.assertIn("APIとlocatorは未変更", "\n".join(output))

    def test_release_action_warns_when_public_audit_skipped_untrusted_entries(self):
        class Workflow:
            def stage(self, source, release_id):
                return {"release_id": release_id, "validation": {
                    "untrusted_subtrees": 2, "untrusted_entries": 3,
                }}
            def switch(self, staged):
                pass
        manager, _, _, output = self.make_manager(
            answers=["/tmp/release", "release-test", "検証配備する", ""]
        )
        manager.release_workflow = Workflow()
        self.assertFalse(manager.deploy_release())
        rendered = "\n".join(output)
        self.assertIn("警告", rendered)
        self.assertIn("信頼できないサブツリー: 2", rendered)
        self.assertIn("信頼できない項目: 3", rendered)

    def test_initial_release_action_calls_bootstrap_migration_with_explicit_confirmation(self):
        calls = []
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /private/old.php", "method": "copy"},
        }
        class Deployment:
            def bootstrap_migrate(self, old_rules, desired, **kwargs):
                calls.append((old_rules, desired, kwargs))
                self.confirmed = kwargs["confirm_maintenance"](120)
                kwargs["switch_pair"]()
        class Workflow:
            PRIVATE_ROOT = "/private/xserver-mail-lineworks"
            deployer = Deployment()
            def stage(self, source, release_id): return {
                "release_id": release_id,
                "release_path": "/home/example/private/releases/" + release_id,
            }
            def switch(self, staged): calls.append(("switch", staged["release_id"]))
        manager, api, config_deployer, _ = self.make_manager(
            answers=["/tmp/release", "release-test", "検証配備する", "初回移行する",
                     "通知停止とメールボックス確認を了承します"],
            config={"notification_targets": [ADDRESS_A], "error_recipients": [ADDRESS_B]},
            initial_command_path="/private/old.php",
        )
        api.snapshot_filters = lambda: [old]
        manager.release_workflow = Workflow()
        self.assertTrue(manager.deploy_release())
        self.assertTrue(Workflow.deployer.confirmed)
        self.assertEqual(manager.command_target, calls[0][1][0]["action"]["target"])
        self.assertEqual(("switch", "release-test"), calls[1])
        self.assertEqual("/private/mail-forward-command", config_deployer.config["command_path"])
        self.assertEqual("release-test", config_deployer.config["release_id"])
        self.assertEqual("/home/example/private/releases/release-test", config_deployer.config["release_path"])

    def test_initial_action_real_deployer_replaces_old_direct_command_with_stable_target(self):
        old_target = "| /usr/bin/php8.5 /home/example/private/old.php"
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": old_target, "method": "copy"},
        }
        api = MigrationApi([old])
        api.is_managed_filter = lambda _rule: False
        release_deployer = ReleaseDeployer(MigrationFtps(), FakeValidator(), api)
        class Workflow:
            PRIVATE_ROOT = "/home/example/private/xserver-mail-lineworks"
            deployer = release_deployer
            def stage(self, source, release_id): return {"release_id": release_id}
            def switch(self, staged): return "switched"
        answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する",
                        "通知停止とメールボックス確認を了承します"])
        manager = MailManager(
            api, FakeDeployer(),
            "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php",
            input_fn=lambda _prompt="": next(answers), output_fn=lambda _message: None,
            config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
            initial_command_path="/home/example/private/old.php",
        )
        self.assertTrue(manager.deploy_release())
        targets = [rule["action"]["target"] for rule in api.rules]
        self.assertEqual([manager.command_target], targets)
        self.assertNotIn(old_target, targets)

    def test_initial_action_retires_both_prior_stable_command_spellings(self):
        stable = "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php"
        wrapper = "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php"
        for old_target in ("|/usr/bin/php8.5 " + stable, "| /usr/bin/php8.5 " + stable):
            with self.subTest(old_target=old_target):
                old = {
                    "id": "old-1", "domain": "example.invalid",
                    "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
                    "action": {"type": "mail_address", "target": old_target, "method": "copy"},
                }
                api = MigrationApi([old])
                release_deployer = ReleaseDeployer(MigrationFtps(), FakeValidator(), api)
                class Workflow:
                    PRIVATE_ROOT = "/home/example/private/xserver-mail-lineworks"
                    deployer = release_deployer
                    def stage(self, source, release_id): return {"release_id": release_id}
                    def switch(self, staged): return "switched"
                answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する",
                                "通知停止とメールボックス確認を了承します"])
                manager = MailManager(
                    api, FakeDeployer(), wrapper,
                    input_fn=lambda _prompt="": next(answers), output_fn=lambda _message: None,
                    config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
                    initial_command_path=stable,
                )
                self.assertTrue(manager.deploy_release())
                self.assertEqual(["| /usr/bin/php8.5 " + wrapper],
                                 [rule["action"]["target"] for rule in api.rules])

    def test_initial_action_preserves_unrelated_rules(self):
        old_target = "| /usr/bin/php8.5 /home/example/private/old.php"
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": old_target, "method": "copy"},
        }
        unrelated = {
            "id": "keep-1", "domain": "example.invalid",
            "conditions": [{"field": "from", "match_type": "match", "keyword": "sender@example.invalid"}],
            "action": {"type": "mail_address", "target": "archive@example.invalid", "method": "copy"},
        }
        api = MigrationApi([old, unrelated])
        release_deployer = ReleaseDeployer(MigrationFtps(), FakeValidator(), api)
        class Workflow:
            PRIVATE_ROOT = "/home/example/private/xserver-mail-lineworks"
            deployer = release_deployer
            def stage(self, source, release_id): return {"release_id": release_id}
            def switch(self, staged): return "switched"
        answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する",
                        "通知停止とメールボックス確認を了承します"])
        manager = MailManager(
            api, FakeDeployer(),
            "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php",
            input_fn=lambda _prompt="": next(answers), output_fn=lambda _message: None,
            config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
            initial_command_path="/home/example/private/old.php",
        )
        self.assertTrue(manager.deploy_release())
        self.assertEqual(unrelated, next(rule for rule in api.rules if rule["id"] == "keep-1"))

    def test_scoped_snapshot_rejects_unknown_id_even_with_desired_body(self):
        old_target = "| /usr/bin/php8.5 /home/example/private/old.php"
        old = rule = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": old_target, "method": "copy"},
        }
        desired = dict(rule, action=dict(rule["action"], target="| /usr/bin/php8.5 /stable.php"))
        desired.pop("id")
        api = MigrationApi([old, dict(desired, id="injected")])
        scoped = _ScopedMigrationApi(api, [old], [old], [desired])
        with self.assertRaises(RuntimeError):
            scoped.snapshot_filters()

    def test_scoped_add_requires_returned_id_in_full_readback(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        desired = dict(old, action=dict(old["action"], target="new"))
        desired.pop("id")
        api = MigrationApi([old])
        api.add_filter = lambda _item: {"id": "claimed-but-absent"}
        scoped = _ScopedMigrationApi(api, [old], [old], [desired])
        with self.assertRaises(RuntimeError):
            scoped.add_filter(desired)

    def test_scoped_old_id_cannot_reappear_after_observed_deletion(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        api = MigrationApi([old])
        scoped = _ScopedMigrationApi(api, [old], [old], [])
        scoped.delete_filter("old-1")
        api.rules = [old]
        with self.assertRaises(RuntimeError):
            scoped.snapshot_filters()

    def test_scoped_delete_requires_post_readback_and_never_authorizes_other_missing_id(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        unrelated = dict(old, id="keep-1", server_field="preserve")
        api = MigrationApi([old, unrelated])
        scoped = _ScopedMigrationApi(api, [old, unrelated], [old], [])
        original = api.delete_filter
        def deletes_both(rule_id):
            result = original(rule_id)
            api.rules = []
            return result
        api.delete_filter = deletes_both
        with self.assertRaises(RuntimeError):
            scoped.delete_filter("old-1")
        mutations = len([call for call in api.calls if call[0] in {"add", "delete"}])
        with self.assertRaises(RuntimeError):
            scoped.snapshot_filters()
        self.assertEqual(mutations, len([call for call in api.calls if call[0] in {"add", "delete"}]))

    def test_scoped_delete_allows_xserver_to_renumber_unrelated_priority(self):
        old = {
            "id": "old-1", "priority": 1, "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        unrelated = {
            "id": "keep-1", "priority": 2, "domain": "example.invalid",
            "conditions": [{"field": "from", "match_type": "match", "keyword": ADDRESS_B}],
            "action": {"type": "mail_address", "target": "archive@example.invalid", "method": "copy"},
        }
        unrelated_two = {
            "id": "keep-2", "priority": 3, "domain": "example.invalid",
            "conditions": [{"field": "from", "match_type": "match", "keyword": ADDRESS_C}],
            "action": {"type": "mail_address", "target": "two@example.invalid", "method": "copy"},
        }
        api = MigrationApi([old, unrelated, unrelated_two])
        original = api.delete_filter

        def delete_and_renumber(rule_id):
            result = original(rule_id)
            for priority, rule in enumerate(api.rules, 1):
                rule["priority"] = priority
            return result

        api.delete_filter = delete_and_renumber
        scoped = _ScopedMigrationApi(api, [old, unrelated, unrelated_two], [old], [])

        scoped.delete_filter("old-1")

        self.assertEqual([("keep-1", 1), ("keep-2", 2)],
                         [(rule["id"], rule["priority"]) for rule in api.rules])

    def test_scoped_delete_rejects_invalid_unrelated_priority_rewrites(self):
        old = {
            "id": "old-1", "priority": 1, "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        keep_one = {
            "id": "keep-1", "priority": 2, "domain": "example.invalid",
            "conditions": [{"field": "from", "match_type": "match", "keyword": ADDRESS_B}],
            "action": {"type": "mail_address", "target": "one@example.invalid", "method": "copy"},
        }
        keep_two = {
            "id": "keep-2", "priority": 3, "domain": "example.invalid",
            "conditions": [{"field": "from", "match_type": "match", "keyword": ADDRESS_C}],
            "action": {"type": "mail_address", "target": "two@example.invalid", "method": "copy"},
        }
        rewrites = {
            "missing": (None, 2),
            "malformed": ("1", 2),
            "swapped": (2, 1),
            "arbitrary": (7, 8),
        }
        for label, priorities in rewrites.items():
            with self.subTest(label=label):
                api = MigrationApi([old, keep_one, keep_two])
                original = api.delete_filter

                def delete_and_rewrite(rule_id):
                    result = original(rule_id)
                    for rule, priority in zip(api.rules, priorities):
                        if priority is None:
                            rule.pop("priority")
                        else:
                            rule["priority"] = priority
                    return result

                api.delete_filter = delete_and_rewrite
                scoped = _ScopedMigrationApi(api, [old, keep_one, keep_two], [old], [])
                with self.assertRaisesRegex(RuntimeError, "priority"):
                    scoped.delete_filter("old-1")

    def test_scoped_delete_api_failure_keeps_pending_authorization_for_exact_recovery(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        api = MigrationApi([old])
        scoped = _ScopedMigrationApi(api, [old], [old], [])
        api.delete_filter = lambda _rule_id: (_ for _ in ()).throw(RuntimeError("failed"))
        with self.assertRaises(RuntimeError): scoped.delete_filter("old-1")
        api.rules = []
        self.assertEqual([], scoped.snapshot_filters())
        self.assertIn("old-1", scoped._journal["retired_ids"])
        self.assertIsNone(scoped._journal["pending"])

    def test_scoped_delete_success_then_readback_exception_recovers_in_fresh_instance(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        api = MigrationApi([old])
        journal = {}
        scoped = _ScopedMigrationApi(api, [old], [old], [], journal=journal)
        original_snapshot = api.snapshot_filters
        calls = 0
        def fail_post_delete():
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise TimeoutError("readback timeout")
            return original_snapshot()
        api.snapshot_filters = fail_post_delete
        with self.assertRaises(TimeoutError): scoped.delete_filter("old-1")
        self.assertEqual({"op": "delete", "id": "old-1"}, journal["pending"])
        api.snapshot_filters = original_snapshot
        recovered = _ScopedMigrationApi(
            api, [old], [old], [], journal=json.loads(json.dumps(journal))
        )
        self.assertEqual([], recovered.snapshot_filters())
        self.assertIn("old-1", recovered._journal["retired_ids"])

    def test_scoped_pending_delete_present_retries_only_exact_body(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        journal = {}
        api = MigrationApi([old])
        scoped = _ScopedMigrationApi(api, [old], [old], [], journal=journal)
        journal["pending"] = {"op": "delete", "id": "old-1"}
        changed = json.loads(json.dumps(old)); changed["action"]["target"] = "changed"
        api.rules = [changed]
        recovered = _ScopedMigrationApi(api, [old], [old], [], journal=json.loads(json.dumps(journal)))
        with self.assertRaisesRegex(RuntimeError, "再利用"):
            recovered.snapshot_filters()
        self.assertFalse(any(call[0] == "delete" for call in api.calls))

    def test_scoped_journal_recovers_deleted_old_rule_in_fresh_process(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        desired = dict(old, action=dict(old["action"], target="new"))
        desired.pop("id")
        journal = {}
        api = MigrationApi([old])
        scoped = _ScopedMigrationApi(api, [old], [old], [desired], journal=journal)
        scoped.delete_filter("old-1")

        recovered = _ScopedMigrationApi(
            api, [old], [old], [desired], journal=json.loads(json.dumps(journal))
        )
        self.assertEqual([], recovered.snapshot_filters())

    def test_scoped_journal_recovery_still_rejects_unrelated_change(self):
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": "old", "method": "copy"},
        }
        unrelated = dict(old, id="keep-1", server_field="preserve")
        journal = {}
        api = MigrationApi([old, unrelated])
        scoped = _ScopedMigrationApi(api, [old, unrelated], [old], [], journal=journal)
        scoped.delete_filter("old-1")
        api.rules[0]["server_field"] = "changed"

        recovered = _ScopedMigrationApi(
            api, [old, unrelated], [old], [], journal=json.loads(json.dumps(journal))
        )
        with self.assertRaisesRegex(RuntimeError, "移行対象外"):
            recovered.snapshot_filters()

    def test_initial_action_injected_unknown_id_has_mutation0_and_locator0(self):
        old_target = "| /usr/bin/php8.5 /home/example/private/old.php"
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": old_target, "method": "copy"},
        }
        api = MigrationApi([old])
        switches = []
        class Deployment:
            api = None
            def bootstrap_migrate(self, *_args, **_kwargs):
                desired = _args[1][0]
                api.rules.append(dict(desired, id="injected"))
                self.api.snapshot_filters()
        class Workflow:
            PRIVATE_ROOT = "/home/example/private/xserver-mail-lineworks"
            deployer = Deployment()
            def stage(self, source, release_id): return {"release_id": release_id}
            def switch(self, staged): switches.append(staged)
        answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する"])
        manager = MailManager(
            api, FakeDeployer(), "/home/example/private/stable.php",
            input_fn=lambda _prompt="": next(answers), output_fn=lambda _message: None,
            config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
            initial_command_path="/home/example/private/old.php",
        )
        with self.assertRaises(RuntimeError):
            manager.deploy_release()
        self.assertEqual(0, len([call for call in api.calls if call[0] in {"add", "delete"}]))
        self.assertEqual([], switches)

    def test_initial_action_rejects_ambiguous_inputs_before_api_mutation(self):
        old_target = "| /usr/bin/php8.5 /home/example/private/old.php"
        def old(rule_id="old-1"):
            return {
                "id": rule_id, "domain": "example.invalid",
                "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
                "action": {"type": "mail_address", "target": old_target, "method": "copy"},
            }
        cases = (
            ([ADDRESS_A, ADDRESS_A], [old()]),
            ([ADDRESS_A], []),
            ([ADDRESS_A], [old("old-1"), old("old-2")]),
            ([ADDRESS_A], [old("")]),
            ([ADDRESS_A], [old("duplicate"), dict(old("duplicate"), domain="other.invalid")]),
        )
        for targets, rules in cases:
            with self.subTest(targets=targets, rules=rules):
                api = MigrationApi(rules)
                class Deployment:
                    def bootstrap_migrate(self, *_args, **_kwargs):
                        raise AssertionError("migration must not start")
                class Workflow:
                    PRIVATE_ROOT = "/home/example/private/xserver-mail-lineworks"
                    deployer = Deployment()
                    def stage(self, source, release_id): return {"release_id": release_id}
                answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する"])
                manager = MailManager(
                    api, FakeDeployer(), "/home/example/private/stable.php",
                    input_fn=lambda _prompt="": next(answers), output_fn=lambda _message: None,
                    config={"notification_targets": targets}, release_workflow=Workflow(),
                    initial_command_path="/home/example/private/old.php",
                )
                with self.assertRaises(RuntimeError):
                    manager.deploy_release()
                self.assertFalse(any(call[0] in ("add", "delete") for call in api.calls))

    def test_real_fresh_manager_recovers_uncertain_delete_from_shared_ftps_journal(self):
        stable_path = "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php"
        wrapper_path = "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php"
        old_target = "|/usr/bin/php8.5 " + stable_path
        old = {
            "id": "old-1", "domain": "example.invalid",
            "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
            "action": {"type": "mail_address", "target": old_target, "method": "copy"},
        }
        api = MigrationApi([old])
        shared_ftps = MigrationFtps()
        validator = FakeValidator()
        switches = []
        class Workflow:
            PRIVATE_ROOT = "/home/example/private/xserver-mail-lineworks"
            def __init__(self): self.deployer = ReleaseDeployer(shared_ftps, validator, api)
            def stage(self, source, release_id): return {"release_id": release_id}
            def switch(self, staged): switches.append(staged["release_id"]); return "switched"
        original_snapshot = api.snapshot_filters
        fail_once = True
        def timeout_after_delete():
            nonlocal fail_once
            if fail_once and not any(rule["id"] == "old-1" for rule in api.rules):
                fail_once = False
                raise TimeoutError("post-delete readback")
            return original_snapshot()
        api.snapshot_filters = timeout_after_delete
        first_answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する",
                              "通知停止とメールボックス確認を了承します"])
        first = MailManager(
            api, FakeDeployer(), wrapper_path,
            input_fn=lambda _prompt="": next(first_answers), output_fn=lambda _message: None,
            config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
            initial_command_path=stable_path,
        )
        with self.assertRaises(TimeoutError): first.deploy_release()
        journal_path = Workflow.PRIVATE_ROOT + "/deploy-transactions/filter-scope.json"
        journal_bytes = shared_ftps.files[journal_path]
        self.assertNotIn(ADDRESS_A.encode(), journal_bytes)
        self.assertNotIn(b"conditions", journal_bytes)
        self.assertIn(("journal-write", "filter"), shared_ftps.calls)

        api.snapshot_filters = original_snapshot
        second_answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する"])
        second = MailManager(
            api, FakeDeployer(), wrapper_path,
            input_fn=lambda _prompt="": next(second_answers), output_fn=lambda _message: None,
            config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
            initial_command_path=stable_path,
        )
        self.assertTrue(second.deploy_release())
        self.assertEqual([second.command_target], [rule["action"]["target"] for rule in api.rules])
        committed = json.loads(shared_ftps.files[journal_path])
        self.assertEqual("committed", committed["phase"])
        self.assertEqual(["release-test"], switches)

    def test_real_fresh_manager_isolates_stale_or_drifted_scope_without_mutation_or_switch(self):
        variants = ("old-body", "unrelated-missing", "unrelated-change", "stale-old-id", "stale-old-hash")
        for variant in variants:
            with self.subTest(variant=variant):
                old = {
                    "id": "old-1", "domain": "example.invalid",
                    "conditions": [{"field": "to", "match_type": "match", "keyword": ADDRESS_A}],
                    "action": {"type": "mail_address", "target": "| /usr/bin/php8.5 /home/example/private/old.php", "method": "copy"},
                }
                unrelated = {
                    "id": "keep-1", "domain": "example.invalid", "server_field": "preserve",
                    "conditions": [{"field": "from", "match_type": "match", "keyword": ADDRESS_B}],
                    "action": {"type": "mail_address", "target": "archive@example.invalid", "method": "copy"},
                }
                api = MigrationApi([old, unrelated])
                shared_ftps = MigrationFtps(); switches = []
                class Workflow:
                    PRIVATE_ROOT = "/home/example/private/xserver-mail-lineworks"
                    def __init__(self): self.deployer = ReleaseDeployer(shared_ftps, FakeValidator(), api)
                    def stage(self, source, release_id): return {"release_id": release_id}
                    def switch(self, staged): switches.append(staged); return "switched"
                original_delete = api.delete_filter
                api.delete_filter = lambda _rule_id: (_ for _ in ()).throw(RuntimeError("uncertain delete"))
                answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する",
                                "通知停止とメールボックス確認を了承します"])
                first = MailManager(
                    api, FakeDeployer(), "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php",
                    input_fn=lambda _prompt="": next(answers), output_fn=lambda _message: None,
                    config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
                    initial_command_path="/home/example/private/old.php",
                )
                with self.assertRaises(RuntimeError): first.deploy_release()
                api.delete_filter = original_delete
                journal_path = Workflow.PRIVATE_ROOT + "/deploy-transactions/filter-scope.json"
                if variant == "old-body": api.rules[0]["action"]["target"] = "changed"
                elif variant == "unrelated-missing": api.rules = [api.rules[0]]
                elif variant == "unrelated-change": api.rules[1]["server_field"] = "changed"
                else:
                    value = json.loads(shared_ftps.files[journal_path])
                    if variant == "stale-old-id": value["old"][0]["id"] = "stale-id"
                    else: value["old"][0]["sha256"] = "f" * 64
                    shared_ftps.files[journal_path] = (json.dumps(value, sort_keys=True,
                        separators=(",", ":")) + "\n").encode()
                mutations = len([call for call in api.calls if call[0] in {"add", "delete"}])
                fresh_answers = iter(["/tmp/release", "release-test", "検証配備する", "初回移行する"])
                fresh = MailManager(
                    api, FakeDeployer(), "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php",
                    input_fn=lambda _prompt="": next(fresh_answers), output_fn=lambda _message: None,
                    config={"notification_targets": [ADDRESS_A]}, release_workflow=Workflow(),
                    initial_command_path="/home/example/private/old.php",
                )
                with self.assertRaises(RuntimeError): fresh.deploy_release()
                self.assertEqual(mutations, len([call for call in api.calls if call[0] in {"add", "delete"}]))
                self.assertEqual([], switches)

    def test_main_requires_explicit_filesystem_home(self):
        environment = {
            "XSERVER_SERVERNAME": "server.example.invalid",
            "XSERVER_COMMAND_PATH": "/mail-lineworks/current/bin/mail-to-lineworks.php",
            "XSERVER_FTPS_HOST": "ftp.example.invalid",
            "XSERVER_CONFIG_PATH": "/mail-lineworks/private/config.json",
        }
        stderr = io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("sys.stderr", stderr):
            self.assertEqual(2, main())
        self.assertIn("XSERVER_HOME", stderr.getvalue())

    def test_main_redacts_ftps_keychain_read_failure(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain.read_ftps_credentials", side_effect=RuntimeError("ftps-secret")), \
                patch("sys.stderr", stderr):
            self.assertEqual(2, main())
        self.assertEqual(
            "認証情報または接続設定を確認できません。キーチェーンと設定を確認してください。\n",
            stderr.getvalue(),
        )
        self.assertNotIn("ftps-secret", stderr.getvalue())

    def test_main_redacts_api_keychain_read_failure(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain.read_ftps_credentials", return_value=("user", "password")), \
                patch("manager.keychain.Keychain.read_api_key", side_effect=OSError("api-secret")), \
                patch("sys.stderr", stderr):
            self.assertEqual(2, main())
        self.assertEqual(
            "認証情報または接続設定を確認できません。キーチェーンと設定を確認してください。\n",
            stderr.getvalue(),
        )
        self.assertNotIn("api-secret", stderr.getvalue())

    def test_main_redacts_unexpected_startup_failure(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain", side_effect=Exception("unexpected-secret")), \
                patch("sys.stderr", stderr):
            self.assertEqual(2, main())
        self.assertEqual("予期しないエラーで管理CLIを開始できませんでした。\n", stderr.getvalue())
        self.assertNotIn("unexpected-secret", stderr.getvalue())

    def test_run_propagates_menu_action_failure_to_main_boundary(self):
        manager, _, _, output = self.make_manager(answers=["1"])
        with patch.object(manager, "list_targets", side_effect=ValueError("menu-secret")), \
                self.assertRaisesRegex(ValueError, "menu-secret"):
            manager.run()
        self.assertNotIn("menu-secret", "\n".join(output))

    def test_main_redacts_propagated_menu_action_failure(self):
        stderr = io.StringIO()
        remote_config = {"error_recipients": [ADDRESS_A]}
        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain.read_ftps_credentials", return_value=("user", "password")), \
                patch("manager.keychain.Keychain.read_api_key", return_value="api-key"), \
                patch("manager.ftps_deployer.FtpsDeployer.read_private_config", return_value=remote_config), \
                patch("manager.private_config_ssh.PrivateConfigSsh.read",
                      return_value=(remote_config, "a" * 64)), \
                patch("manager.manage.MailManager.run", side_effect=ValueError("menu-secret")), \
                patch("sys.stderr", stderr):
            self.assertEqual(2, main())
        self.assertEqual(
            "認証情報または接続設定を確認できません。キーチェーンと設定を確認してください。\n",
            stderr.getvalue(),
        )
        self.assertNotIn("menu-secret", stderr.getvalue())

    def test_main_constructs_live_remote_validator_release_deployer_and_workflow(self):
        remote_config = {"error_recipients": [ADDRESS_A]}
        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain.read_ftps_credentials", return_value=("user", "password")), \
                patch("manager.keychain.Keychain.read_api_key", return_value="api-key"), \
                patch("manager.ftps_deployer.FtpsDeployer.read_private_config", return_value=remote_config), \
                patch("manager.private_config_ssh.PrivateConfigSsh.read",
                      return_value=(remote_config, "a" * 64)), \
                patch("manager.manage.MailManager") as manager_class:
            self.assertEqual(0, main())
        workflow = manager_class.call_args.kwargs["release_workflow"]
        self.assertEqual("safe-alias", workflow.deployer.remote_validator.ssh_alias)
        self.assertEqual(
            Path.home() / ".ssh" / "xserver-mail-lineworks.conf",
            workflow.deployer.remote_validator.config,
        )
        self.assertIs(workflow.deployer.api, manager_class.call_args.args[0])
        self.assertEqual("/home/example/mail-lineworks/private/config.json", workflow.config_path)
        self.assertEqual(
            "/home/example/mail-lineworks/private/config.json",
            workflow.deployer.validation_context["config_path"],
        )
        self.assertEqual(
            "/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php",
            manager_class.call_args.args[2],
        )
        private_client = manager_class.call_args.kwargs["private_config_client"]
        self.assertEqual(["ftp.example.invalid", "server.example.invalid"],
                         private_client.expected_hosts)
        manager_class.return_value.run.assert_called_once_with()

    def test_main_reads_initial_private_config_over_ssh_when_ftps_retr_is_denied(self):
        remote_config = {
            "webhook_url": self.OLD_URL,
            "error_recipients": [ADDRESS_A],
            "log_path": "/home/example/mail-lineworks/private/notifier.jsonl",
        }
        private_client = unittest.mock.Mock()
        private_client.read.return_value = (remote_config, "a" * 64)
        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain.read_ftps_credentials", return_value=("user", "password")), \
                patch("manager.keychain.Keychain.read_api_key", return_value="api-key"), \
                patch("manager.ftps_deployer.FtpsDeployer.read_private_config",
                      side_effect=RuntimeError("RETR 600 denied")), \
                patch("manager.private_config_ssh.PrivateConfigSsh", return_value=private_client), \
                patch("manager.manage.MailManager") as manager_class:
            self.assertEqual(0, main())
        private_client.read.assert_called_once_with()
        self.assertEqual(remote_config, manager_class.call_args.kwargs["config"])
        self.assertEqual([ADDRESS_A], manager_class.call_args.kwargs["error_recipients"])

    def test_legacy_app_and_server_bootstraps_strict_missing_helper_before_config_read(self):
        remote_config = {
            "webhook_url": self.OLD_URL, "error_recipients": [ADDRESS_A],
            "log_path": "/home/example/mail-lineworks/private/notifier.jsonl",
        }
        private_client = unittest.mock.Mock()
        private_client.read.side_effect = [RuntimeError("fixed helper unavailable"),
                                           (remote_config, "a" * 64)]
        class BootstrapFtps:
            def __init__(self):
                self.hash_checks = []
                self.replacements = []
            def verify_private_file_hashes(self, expected):
                self.hash_checks.append(expected)
                return True
            def replace_bytes_atomic(self, path, body, *, mode):
                raise RuntimeError("550 helper temp write denied")
            def verify_private_files(self, expected):
                return True
        class BootstrapValidator:
            def __init__(self):
                self.states = iter(("PARTIAL", "EXACT", "EXACT"))
                self.inspections = []
                self.provisions = []
            def inspect_fixed_runtime(self, root, entries, *, expected_hosts):
                self.inspections.append((root, entries, expected_hosts))
                return next(self.states)
            def provision_fixed_helper(self, root, relative, body, *, expected_sha256,
                                       mode, expected_hosts):
                self.provisions.append((root, relative, body, expected_sha256,
                                        mode, expected_hosts))
                return "changed"
        ftps = BootstrapFtps()
        validator = BootstrapValidator()
        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain.read_ftps_credentials", return_value=("user", "password")), \
                patch("manager.keychain.Keychain.read_api_key", return_value="api-key"), \
                patch("manager.ftps_deployer.FtpsDeployer", return_value=ftps), \
                patch("manager.remote_validator.RemoteValidator", return_value=validator), \
                patch("manager.private_config_ssh.PrivateConfigSsh", return_value=private_client), \
                patch("builtins.input", return_value="秘密設定helperを初期配備する"), \
                patch("manager.manage.MailManager") as manager_class:
            self.assertEqual(0, main())
        helper_path, manifest_path, mode = _legacy_bootstrap_asset_paths()
        repository = Path(__file__).resolve().parents[2]
        self.assertEqual((repository / "bin/manage-private-config.php",
                          repository / "fixed-runtime/legacy-manifest.json", 0o644),
                         (helper_path, manifest_path, mode))
        self.assertEqual([], ftps.replacements)
        helper_body = helper_path.read_bytes()
        self.assertEqual([(
            "/home/example/private/xserver-mail-lineworks",
            "bootstrap/manage-private-config.php", helper_body,
            __import__("hashlib").sha256(helper_body).hexdigest(), 0o700,
            ["ftp.example.invalid", "server.example.invalid"],
        )], validator.provisions)
        self.assertEqual(json.loads(manifest_path.read_text())["entries"],
                         validator.inspections[1][1])
        self.assertEqual([], ftps.hash_checks)
        self.assertEqual(2, private_client.read.call_count)
        manager_class.return_value.run.assert_called_once_with()

    def test_main_reaches_menu_14_and_15_with_ssh_config_when_ftps_retr_is_denied(self):
        initial = {
            "webhook_url": self.OLD_URL, "error_recipients": [ADDRESS_A],
            "log_path": "/home/example/mail-lineworks/private/notifier.jsonl",
        }
        changed = dict(initial, webhook_url=self.NEW_URL)
        private_client = unittest.mock.Mock()
        private_client.read.side_effect = [
            (initial, "a" * 64), (initial, "a" * 64),
            (initial, "a" * 64), (changed, "b" * 64),
        ]
        private_client.compare_and_swap.return_value = ConfigCasResult(
            "changed", "a" * 64, "b" * 64)
        answers = iter(["14", "", "15", self.NEW_URL,
                        "Webhook URLを変更する", "0"])
        output = []
        original_run = MailManager.run

        def run_menu(instance):
            instance.input = lambda _prompt="": next(answers)
            instance.output = output.append
            instance.webhook_sender = lambda _url, _payload: 200
            return original_run(instance)

        with patch.dict("os.environ", self.MAIN_ENVIRONMENT, clear=True), \
                patch("manager.keychain.Keychain.read_ftps_credentials", return_value=("user", "password")), \
                patch("manager.keychain.Keychain.read_api_key", return_value="api-key"), \
                patch("manager.ftps_deployer.FtpsDeployer.read_private_config",
                      side_effect=RuntimeError("RETR 600 denied")), \
                patch("manager.private_config_ssh.PrivateConfigSsh", return_value=private_client), \
                patch.object(MailManager, "run", autospec=True, side_effect=run_menu):
            self.assertEqual(0, main())
        self.assertEqual(4, private_client.read.call_count)
        private_client.compare_and_swap.assert_called_once()
        self.assertIn("Webhook URLの変更を確認しました。", output)


if __name__ == "__main__":
    unittest.main()
