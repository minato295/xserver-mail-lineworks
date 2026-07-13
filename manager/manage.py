#!/usr/bin/env python3
"""日本語対話式の XServer メール通知管理 CLI。"""

import os
import base64
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlparse


_MAX_RECIPIENTS = 32
_MAX_TO_BYTES = 900
_MAX_LOG_PATH_BYTES = 4096
_MAX_HEADER_LINE_BYTES = 997
_MAX_SIGNED_MESSAGE_BYTES = 65535
# Fixed upper bounds for every non-configurable byte in one unfolded header
# and in the complete re-authenticatable system message. Configurable To/log
# bytes are added explicitly by _runtime_config_sizes().
_SYSTEM_MAIL_FIXED_HEADER_LINE_BYTES = 160
_SYSTEM_MAIL_FIXED_MESSAGE_BYTES = 8192

try:
    from manager.email_address import CanonicalEmailError, canonical_email, canonical_email_list
    from manager.xserver_api import build_command_target
    from manager.scope_journal import ScopeJournal
    from manager.ftps_deployer import _build_verified_ssl_context
except ModuleNotFoundError:  # Support `python3 manager/manage.py` from the repository root.
    from email_address import CanonicalEmailError, canonical_email, canonical_email_list
    from xserver_api import build_command_target
    from scope_journal import ScopeJournal
    from ftps_deployer import _build_verified_ssl_context


def _filesystem_config_path(filesystem_home, ftps_config_path):
    """Combine separately rooted private paths without normalization ambiguity."""
    for value in (filesystem_home, ftps_config_path):
        if (not isinstance(value, str) or not value.startswith("/") or value == "/"
                or "\x00" in value or "//" in value
                or any(part in ("", ".", "..") or part.casefold() == "public_html"
                       for part in value.split("/")[1:])):
            raise ValueError("remote private path is invalid")
    return filesystem_home.rstrip("/") + ftps_config_path


def _legacy_bootstrap_asset_paths(manager_file=__file__):
    """Resolve the two supported signed-code layouts without probing alternatives."""
    manager_directory = Path(manager_file).absolute().parent
    container = manager_directory.parent
    if manager_directory.name == "manager" and container.name == "Resources":
        fixed = container / "fixed-runtime"
        return (fixed / "manage-private-config.php",
                fixed / "legacy-manifest.json", 0o600)
    repository = container
    return (repository / "bin/manage-private-config.php",
            repository / "fixed-runtime/legacy-manifest.json", 0o644)


def validate_email(value):
    """Return one syntactically valid address, rejecting header injection."""
    try:
        return canonical_email(value)
    except CanonicalEmailError:
        if type(value) is str and ("\r" in value or "\n" in value):
            raise ValueError("メールアドレスに改行は使用できません") from None
        raise ValueError("メールアドレスの形式が正しくありません") from None


def validate_webhook_url(value):
    """Return the canonical LINE WORKS webhook URL or reject it."""
    if type(value) is not str:
        raise RuntimeError("Webhook URLが不正です")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if parsed is None:
        raise RuntimeError("Webhook URLが不正です")
    token = (parsed.path.removeprefix("/message/")
             if parsed.path.startswith("/message/") else "")
    if not (
        parsed.scheme == "https"
        and parsed.hostname == "webhook.worksmobile.com"
        and parsed.username is None and parsed.password is None and port is None
        and parsed.path == "/message/" + token
        and re.fullmatch(r"[A-Za-z0-9._~-]+", token) is not None
        and not parsed.query and not parsed.fragment
        and value == "https://webhook.worksmobile.com/message/" + token
    ):
        raise RuntimeError("Webhook URLが不正です")
    return value


def mask_webhook_url(value: str) -> str:
    """Mask the complete secret token while retaining the non-secret endpoint shape."""
    validate_webhook_url(value)
    token = value.rsplit("/", 1)[1]
    masked = "*" * len(token) if len(token) <= 4 else "********…" + token[-4:]
    return "https://webhook.worksmobile.com/message/" + masked


def _send_webhook_exact_status(webhook_url, payload):
    request = Request(
        webhook_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=15, context=_build_verified_ssl_context()) as response:
        status = getattr(response, "status", None)
        response.read()
    return status


class _ScopedMigrationApi:
    """Expose only one prevalidated migration set while auditing every full readback."""

    def __init__(self, api, all_rules, old_rules, desired_rules, *, journal=None,
                 persist_journal=None):
        self._api = api
        self._baseline_by_id = {
            rule["id"]: json.loads(json.dumps(rule, ensure_ascii=False)) for rule in all_rules
        }
        self._old_by_id = {rule["id"]: self._body(rule) for rule in old_rules}
        self._active_old_ids = set(self._old_by_id)
        self._new_by_id = {}
        self._confirmed_new_ids = set()
        self._retired_ids = set()
        self._pending_delete = None
        self._desired = {self._body(rule) for rule in desired_rules}
        self._unrelated = {
            rule["id"]: self._canonical(rule) for rule in all_rules
            if rule["id"] not in self._old_by_id
        }
        self._journal = journal if journal is not None else {}
        self._persist_journal = persist_journal or (lambda _state: None)
        scope = {
            "old": sorted((rule["id"], self._digest(self._body(rule))) for rule in old_rules),
            "desired": sorted(self._digest(self._body(rule)) for rule in desired_rules),
            "unrelated": sorted((rule_id, self._digest(value))
                                for rule_id, value in self._unrelated.items()),
        }
        scope_hash = self._digest(json.dumps(scope, sort_keys=True, separators=(",", ":")))
        if self._journal:
            if (set(self._journal) - {"version", "scope_sha256", "retired_ids", "new_ids",
                                     "pending"}
                    or self._journal.get("version") != 1
                    or self._journal.get("scope_sha256") != scope_hash):
                raise RuntimeError("移行journalのscopeが一致しません")
        else:
            self._journal.update({"version": 1, "scope_sha256": scope_hash,
                                  "retired_ids": [], "new_ids": {}, "pending": None})
            self._save_journal()
        self._retired_ids.update(self._journal.get("retired_ids", []))
        self._active_old_ids -= self._retired_ids
        for rule_id, body_hash in self._journal.get("new_ids", {}).items():
            matches = [body for body in self._desired if self._digest(body) == body_hash]
            if len(matches) != 1:
                raise RuntimeError("移行journalの追加filterが不正です")
            self._new_by_id[rule_id] = matches[0]
            self._confirmed_new_ids.add(rule_id)

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _save_journal(self):
        self._journal["retired_ids"] = sorted(self._retired_ids)
        self._journal["new_ids"] = {
            rule_id: self._digest(body) for rule_id, body in sorted(self._new_by_id.items())
        }
        self._persist_journal(dict(self._journal))

    @staticmethod
    def _body(rule):
        return json.dumps(
            {key: rule.get(key) for key in ("domain", "conditions", "action")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    @staticmethod
    def _canonical(rule):
        return json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _canonical_without_priority(cls, rule):
        return cls._canonical({key: value for key, value in rule.items() if key != "priority"})

    def _expected_compacted_priority(self, rule_id, present_ids, authorized_delete):
        baseline = self._baseline_by_id[rule_id]
        domain = baseline.get("domain")
        peers = [rule for rule in self._baseline_by_id.values() if rule.get("domain") == domain]
        priorities = [rule.get("priority") for rule in peers]
        if (any(not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in priorities)
                or len(priorities) != len(set(priorities))
                or sorted(priorities) != list(range(1, len(priorities) + 1))):
            return None
        missing = {rule["id"] for rule in peers if rule["id"] not in present_ids}
        allowed_missing = set(self._retired_ids)
        if authorized_delete is not None:
            allowed_missing.add(authorized_delete)
        if not missing or not missing <= allowed_missing:
            return None
        return baseline["priority"] - sum(
            1 for rule in peers
            if rule["id"] in missing and rule["priority"] < baseline["priority"]
        )

    def snapshot_filters(self):
        rules = self._api.snapshot_filters()
        ids = [rule.get("id") for rule in rules]
        if (any(not isinstance(rule_id, str) or not rule_id for rule_id in ids)
                or len(ids) != len(set(ids))):
            raise RuntimeError("API filter IDを一意に確認できません")
        current = {rule["id"]: self._canonical(rule) for rule in rules}
        known_ids = set(self._unrelated) | set(self._old_by_id) | set(self._new_by_id)
        unknown = [rule for rule in rules if rule["id"] not in known_ids]
        pending = self._journal.get("pending")
        if unknown and isinstance(pending, dict) and pending.get("op") == "add":
            matching = [rule for rule in unknown
                        if self._digest(self._body(rule)) == pending.get("body_sha256")]
            if len(unknown) == len(matching) == 1:
                adopted = matching[0]
                self._new_by_id[adopted["id"]] = self._body(adopted)
                self._confirmed_new_ids.add(adopted["id"])
                self._journal["pending"] = None
                self._save_journal()
                known_ids.add(adopted["id"])
        if any(rule_id not in known_ids for rule_id in current):
            raise RuntimeError("移行対象外のfilterが追加されました")
        present_ids = set(current)
        journal_pending = self._journal.get("pending")
        authorized_delete = self._pending_delete
        if authorized_delete is None and isinstance(journal_pending, dict) \
                and journal_pending.get("op") == "delete":
            authorized_delete = journal_pending.get("id")
        rules_by_id = {rule["id"]: rule for rule in rules}
        for rule_id, canonical in self._unrelated.items():
            rule = rules_by_id.get(rule_id)
            if rule is None:
                raise RuntimeError("移行対象外のfilterが変更されました")
            if self._canonical(rule) == canonical:
                continue
            baseline = self._baseline_by_id[rule_id]
            if self._canonical_without_priority(rule) != self._canonical_without_priority(baseline):
                raise RuntimeError("移行対象外のfilterが変更されました")
            expected = self._expected_compacted_priority(
                rule_id, present_ids, authorized_delete
            )
            actual = rule.get("priority")
            if (not isinstance(actual, int) or isinstance(actual, bool) or actual != expected):
                raise RuntimeError("移行対象外のfilter priorityが不正です")
        for rule_id, expected_body in self._old_by_id.items():
            if rule_id in self._retired_ids and rule_id in present_ids:
                raise RuntimeError("filter IDが再利用されました")
            if rule_id in present_ids and self._body(next(
                    rule for rule in rules if rule["id"] == rule_id)) != expected_body:
                raise RuntimeError("filter IDが再利用されました")
        disappeared_old = self._active_old_ids - present_ids
        if disappeared_old:
            if disappeared_old != {authorized_delete}:
                raise RuntimeError("認可されていない旧filter欠落を検出しました")
            self._active_old_ids -= disappeared_old
            self._retired_ids.update(disappeared_old)
            self._journal["pending"] = None
            self._save_journal()
        for rule_id, expected_body in self._new_by_id.items():
            if rule_id in self._retired_ids and rule_id in present_ids:
                raise RuntimeError("filter IDが再利用されました")
            if rule_id in present_ids:
                rule = next(rule for rule in rules if rule["id"] == rule_id)
                if self._body(rule) != expected_body:
                    raise RuntimeError("追加filterのreadbackが一致しません")
            elif rule_id in self._confirmed_new_ids:
                if rule_id != self._pending_delete:
                    raise RuntimeError("認可されていない新filter欠落を検出しました")
                self._retired_ids.add(rule_id)
        return [rule for rule in rules
                if rule["id"] in self._old_by_id or rule["id"] in self._new_by_id]

    def add_filter(self, rule):
        self.snapshot_filters()
        if self._body(rule) not in self._desired:
            raise RuntimeError("移行対象外のfilter追加を拒否しました")
        self._journal["pending"] = {"op": "add", "body_sha256": self._digest(self._body(rule))}
        self._save_journal()
        result = self._api.add_filter(rule)
        rule_id = result.get("id") if isinstance(result, dict) else None
        known_ids = set(self._unrelated) | set(self._old_by_id) | set(self._new_by_id)
        if not isinstance(rule_id, str) or not rule_id or rule_id in known_ids \
                or rule_id in self._retired_ids:
            raise RuntimeError("追加filter IDを一意に確認できません")
        self._new_by_id[rule_id] = self._body(rule)
        self._save_journal()
        readback = self.snapshot_filters()
        if not any(item["id"] == rule_id and self._body(item) == self._body(rule)
                   for item in readback):
            raise RuntimeError("追加filterをreadbackで確認できません")
        self._confirmed_new_ids.add(rule_id)
        self._journal["pending"] = None
        self._save_journal()
        return result

    def delete_filter(self, rule_id):
        self.snapshot_filters()
        if rule_id not in self._old_by_id and rule_id not in self._new_by_id:
            raise RuntimeError("移行対象外のfilter削除を拒否しました")
        if self._pending_delete is not None:
            raise RuntimeError("filter削除状態が競合しました")
        self._pending_delete = rule_id
        self._journal["pending"] = {"op": "delete", "id": rule_id}
        self._save_journal()
        try:
            result = self._api.delete_filter(rule_id)
            if any(rule["id"] == rule_id for rule in self.snapshot_filters()):
                raise RuntimeError("削除filterがreadbackに残っています")
            return result
        finally:
            self._pending_delete = None


class MailManager:
    MENU = """\
1. 恒久通知対象一覧
2. 恒久通知対象追加（複数可）
3. 恒久通知対象変更
4. 恒久通知対象削除
5. エラー通知先一覧
6. エラー通知先追加（複数可）
7. エラー通知先変更
8. エラー通知先削除
9. 同期診断
10. LINE WORKSテスト
11. エラーメールテスト
12. 新リリースを検証・配備
13. 通知対象を転送設定から同期
14. Webhook URL確認
15. Webhook URL変更
0. 終了"""

    def __init__(self, api, deployer, command_path, *, input_fn=input, output_fn=print,
                 error_recipients=None, diagnostic_fn=None, lineworks_test_fn=None,
                 error_mail_test_fn=None, config=None, now_fn=None, test_token_fn=None,
                 release_workflow=None, initial_command_path=None,
                 private_config_client=None, webhook_sender=None):
        self.api = api
        self.deployer = deployer
        self.command_path = command_path
        self.command_target = build_command_target(command_path)
        self.input = input_fn
        self.output = output_fn
        self.error_recipients = list(error_recipients or [])
        self.config = dict(config or {})
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.test_token_fn = test_token_fn or (lambda: "ERRTEST-" + secrets.token_urlsafe(24))
        self.diagnostic_fn = diagnostic_fn or self._default_diagnostics
        self.lineworks_test_fn = lineworks_test_fn or self._default_lineworks_test
        self.error_mail_test_fn = error_mail_test_fn or self._default_error_mail_test
        self.release_workflow = release_workflow
        self.private_config_client = private_config_client
        if (self.private_config_client is None and callable(getattr(deployer, "read", None))
                and callable(getattr(deployer, "compare_and_swap", None))):
            self.private_config_client = deployer
        self.webhook_sender = webhook_sender or _send_webhook_exact_status
        self.initial_command_targets = (() if not initial_command_path else (
            "|/usr/bin/php8.5 " + initial_command_path,
            build_command_target(initial_command_path),
        ))

    def _managed(self):
        return [rule for rule in self.api.list_filters() if self.api.is_managed_filter(rule)]

    @staticmethod
    def _address(rule):
        return rule["conditions"][0]["keyword"]

    def _find(self, address):
        return next((rule for rule in self._managed() if self._address(rule) == address), None)

    def _read_addresses(self, prompt):
        raw = self.input(prompt)
        values = [validate_email(item) for item in raw.split(",") if item.strip()]
        if not values:
            raise ValueError("メールアドレスを1件以上入力してください")
        return canonical_email_list(values, allow_empty=False)

    @staticmethod
    def _runtime_config_sizes(config):
        recipients = config["error_recipients"]
        to_bytes = len(",".join(recipients).encode("ascii"))
        log_bytes = len(config["log_path"].encode("utf-8"))
        return {
            "recipient_count": len(recipients),
            "to_bytes": to_bytes,
            "log_path_bytes": log_bytes,
            "header_line_bytes": max(
                _SYSTEM_MAIL_FIXED_HEADER_LINE_BYTES, len(b"To: ") + to_bytes
            ),
            "signed_message_bytes": (
                _SYSTEM_MAIL_FIXED_MESSAGE_BYTES + to_bytes + log_bytes
            ),
        }

    @staticmethod
    def _validate_runtime_config(config, *, strict):
        """Return a bounded canonical runtime config without discarding unknown keys."""
        if type(config) is not dict:
            raise RuntimeError("秘密設定を更新できません。管理者へ連絡してください。")
        try:
            recipients = canonical_email_list(
                config.get("error_recipients"), allow_empty=False,
                reject_duplicates=strict,
            )
            targets = canonical_email_list(
                config.get("notification_targets", []), allow_empty=True,
                reject_duplicates=strict,
            )
            pinned = canonical_email_list(
                config.get("notification_pinned_targets", []), allow_empty=True,
                reject_duplicates=strict,
            )
        except CanonicalEmailError:
            raise RuntimeError("通知先設定が不正です。管理者へ連絡してください。") from None
        result = dict(config)
        result["error_recipients"] = recipients
        result["notification_targets"] = targets
        result["notification_pinned_targets"] = pinned
        log_path = config.get("log_path")
        if type(log_path) is not str or not log_path.startswith("/"):
            raise RuntimeError("ログ保存先を4096バイト以下の安全な絶対パスへ変更してください。")
        sizes = MailManager._runtime_config_sizes(result)
        if sizes["recipient_count"] > _MAX_RECIPIENTS:
            raise RuntimeError("エラー通知先は合計32件以下に減らしてから再実行してください。")
        if sizes["to_bytes"] > _MAX_TO_BYTES:
            raise RuntimeError("エラー通知先の合計長を900バイト以下に減らしてから再実行してください。")
        if sizes["log_path_bytes"] > _MAX_LOG_PATH_BYTES:
            raise RuntimeError("ログ保存先を4096バイト以下の安全な絶対パスへ変更してください。")
        if (sizes["header_line_bytes"] > _MAX_HEADER_LINE_BYTES
                or sizes["signed_message_bytes"] > _MAX_SIGNED_MESSAGE_BYTES):
            raise RuntimeError("システム通知の安全なサイズ上限を超えています。管理者へ連絡してください。")
        key = config.get("system_mail_hmac_key")
        if key is not None:
            try:
                decoded = base64.urlsafe_b64decode(key + "=") if (
                    type(key) is str and len(key) == 43
                    and re.fullmatch(r"[A-Za-z0-9_-]{43}", key)) else b""
            except (ValueError, TypeError):
                decoded = b""
            if (len(decoded) != 32
                    or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != key):
                raise RuntimeError("システムメール認証鍵が不正です。管理者へ連絡してください。")
        test_until_key = "test_force_webhook_failure_until"
        test_token_key = "test_error_subject_token"
        if (test_until_key in config) != (test_token_key in config):
            raise RuntimeError("エラーメールテスト設定が不正です。管理者へ連絡してください。")
        if (test_until_key in config
                and (config[test_until_key] is not None or config[test_token_key] is not None)):
            test_until = config[test_until_key]
            test_token = config[test_token_key]
            if (type(test_token) is not str
                    or re.fullmatch(r"ERRTEST-[A-Za-z0-9_-]{24,120}", test_token) is None
                    or type(test_until) is not str
                    or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", test_until) is None):
                raise RuntimeError("エラーメールテスト設定が不正です。管理者へ連絡してください。")
            try:
                parsed_test_until = datetime.fromisoformat(test_until)
            except ValueError:
                parsed_test_until = None
            if (parsed_test_until is None
                    or parsed_test_until.isoformat(timespec="seconds") != test_until):
                raise RuntimeError("エラーメールテスト設定が不正です。管理者へ連絡してください。")
        return result

    def ensure_runtime_config(self):
        """Upgrade the pre-release private schema using the currently deployed CAS helper."""
        current, current_sha256 = self._read_private_config()
        updated = self._validate_runtime_config(current, strict=False)
        if "system_mail_hmac_key" not in updated:
            updated["system_mail_hmac_key"] = base64.urlsafe_b64encode(
                secrets.token_bytes(32)
            ).rstrip(b"=").decode("ascii")
        updated = self._validate_runtime_config(updated, strict=True)
        if updated == current:
            self.config = dict(updated)
            self.error_recipients = list(updated["error_recipients"])
            return True
        if not self._replace_private_config(current_sha256, updated):
            raise RuntimeError("秘密設定が競合しました。再起動してやり直してください。")
        self.config = dict(updated)
        self.error_recipients = list(updated["error_recipients"])
        return True

    def _rule(self, address):
        return {
            "domain": address.rsplit("@", 1)[1],
            "conditions": [{"field": "header", "match_type": "contain", "keyword": address}],
            "action": {"type": "mail_address", "target": self.command_target, "method": "copy"},
        }

    def _legacy_managed(self, rule):
        conditions = rule.get("conditions", [])
        action = rule.get("action", {})
        if len(conditions) != 1:
            return False
        condition = conditions[0]
        try:
            address = validate_email(condition.get("keyword"))
        except (TypeError, ValueError):
            return False
        return (condition.get("field") == "to"
                and condition.get("match_type") == "match"
                and rule.get("domain") == address.rsplit("@", 1)[1]
                and action.get("type") == "mail_address"
                and action.get("target") == self.command_target
                and action.get("method") == "copy")

    def _legacy_rule(self, address):
        rule = self._rule(address)
        rule["conditions"] = [{"field": "to", "match_type": "match", "keyword": address}]
        return rule

    def expected_targets(self):
        base = self.config.get("notification_base_address")
        try:
            base = validate_email(base)
        except (TypeError, ValueError) as error:
            raise RuntimeError("通知基準アドレスを確認できません") from error
        targets = self.api.discover_forwarding_sources(base)
        try:
            canonical = [validate_email(address) for address in targets]
        except (TypeError, ValueError) as error:
            raise RuntimeError("転送元アドレスを完全に読み取れません") from error
        if not canonical or canonical != sorted(set(canonical)) or base not in canonical:
            raise RuntimeError("転送元アドレスを一意に確認できません")
        return canonical

    def plan_target_sync(self, expected=None):
        expected = self.expected_targets() if expected is None else canonical_email_list(
            expected, allow_empty=False, reject_duplicates=True)
        rules = self.api.snapshot_filters()
        ids = [rule.get("id") for rule in rules]
        if (any(not isinstance(rule_id, str) or not rule_id for rule_id in ids)
                or len(ids) != len(set(ids))):
            raise RuntimeError("API filter IDを一意に確認できません")
        managed = [rule for rule in rules if self.api.is_managed_filter(rule)]
        legacy = [rule for rule in rules if self._legacy_managed(rule)]
        addresses = [self._address(rule) for rule in managed]
        if len(addresses) != len(set(addresses)):
            raise RuntimeError("管理対象filterが競合しています")
        return {
            "add": [address for address in expected if address not in addresses],
            "delete": [self._address(rule) for rule in managed if self._address(rule) not in expected]
                      + [self._address(rule) for rule in legacy],
            "expected": expected,
            "rules": rules,
            "managed": managed,
            "retire": [rule for rule in managed if self._address(rule) not in expected] + legacy,
        }

    def _print_target_sync_diff(self, plan):
        for address in plan["add"]:
            self.output("+ " + address)
        for address in plan["delete"]:
            self.output("- " + address)
        if not plan["add"] and not plan["delete"]:
            self.output("通知対象の差分はありません")

    @staticmethod
    def _verify_committable_scope(durable, rules):
        """Require the exact journal-authorized final API identity and bodies."""
        expected_ids = ({item["id"] for item in durable["unrelated"]}
                        | {item["id"] for item in durable["new_ids"]})
        current_by_id = {rule.get("id"): rule for rule in rules}
        if (None in current_by_id or len(current_by_id) != len(rules)
                or set(current_by_id) != expected_ids
                or {item["id"] for item in durable["old"]} != set(durable["retired_ids"])
                or sorted(item["sha256"] for item in durable["new_ids"])
                   != sorted(durable["new"])):
            raise RuntimeError("通知対象同期の最終API baselineが一致しません")
        for item in durable["unrelated"]:
            if ScopeJournal.digest(current_by_id[item["id"]]) != item["sha256"]:
                raise RuntimeError("通知対象同期の最終API baselineが一致しません")
        for item in durable["new_ids"]:
            if ScopeJournal.digest(ScopeJournal.body(
                    current_by_id[item["id"]])) != item["sha256"]:
                raise RuntimeError("通知対象同期の最終API baselineが一致しません")

    @staticmethod
    def _canonical_filter_snapshot(rules):
        if not isinstance(rules, list):
            raise RuntimeError("API filter snapshotを完全に確認できません")
        result = []
        ids = set()
        for rule in rules:
            if not isinstance(rule, dict):
                raise RuntimeError("API filter snapshotを完全に確認できません")
            rule_id = rule.get("id")
            if not isinstance(rule_id, str) or not rule_id or rule_id in ids:
                raise RuntimeError("API filter IDを一意に確認できません")
            ids.add(rule_id)
            try:
                canonical = json.dumps(rule, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"))
            except (TypeError, ValueError):
                raise RuntimeError("API filter snapshotを完全に確認できません") from None
            result.append((rule_id, canonical))
        return tuple(sorted(result))

    def _close_legacy_v1_target_journal(self, *, remote, remote_sha256,
                                        durable, durable_bytes, journal_store):
        pending = durable.get("pending")
        old_hashes = {item["id"]: item["sha256"] for item in durable["old"]}
        retired_ids = set(durable["retired_ids"])
        if (durable.get("phase") != "active" or not isinstance(pending, dict)
                or pending.get("kind") != "delete"
                or pending.get("id") not in old_hashes
                or pending.get("sha256") != old_hashes.get(pending.get("id"))
                or not retired_ids.issubset(old_hashes)
                or pending.get("id") in retired_ids):
            raise RuntimeError("旧形式の未完了同期journalがあるため変更できません")
        try:
            configured = canonical_email_list(
                remote.get("notification_targets"), allow_empty=False,
                reject_duplicates=True)
            automatic = self.expected_targets()
        except CanonicalEmailError:
            raise RuntimeError("旧同期journalの通知対象設定を確認できません") from None
        intended_hashes = sorted(
            ScopeJournal.digest(ScopeJournal.body(self._rule(address)))
            for address in automatic)
        if configured != automatic or intended_hashes != sorted(durable["new"]):
            raise RuntimeError("旧同期journalの通知対象baselineが一致しません")

        clean_plan = self.plan_target_sync(automatic)
        initial_rules = clean_plan["rules"]
        initial_snapshot = self._canonical_filter_snapshot(initial_rules)
        current = {rule["id"]: rule for rule in initial_rules}
        if set(old_hashes) & set(current):
            raise RuntimeError("旧同期journalの削除対象IDが残っています")
        if sorted(item["sha256"] for item in durable["new_ids"]) \
                != sorted(durable["new"]):
            raise RuntimeError("旧同期journalの新filter hash集合が一致しません")
        for item in durable["new_ids"]:
            rule = current.get(item["id"])
            if (rule is None or ScopeJournal.digest(ScopeJournal.body(rule))
                    != item["sha256"]):
                raise RuntimeError("旧同期journalの新filter baselineが一致しません")
        missing_unrelated = 0
        for item in durable["unrelated"]:
            rule = current.get(item["id"])
            if rule is None:
                missing_unrelated += 1
            elif ScopeJournal.digest(rule) != item["sha256"]:
                raise RuntimeError("旧同期journalの対象外filter baselineが一致しません")
        known = ({item["id"] for item in durable["old"]}
                 | {item["id"] for item in durable["new_ids"]}
                 | {item["id"] for item in durable["unrelated"]})
        unknown_ids = set(current) - known
        clean_retire_ids = {item["id"] for item in clean_plan["retire"]}
        if clean_plan["add"] or unknown_ids != clean_retire_ids:
            raise RuntimeError("旧同期journal外のAPI filterを安全に分類できません")
        if unknown_ids:
            self.output("警告: journal外の現行filter: %d件" % len(unknown_ids))
        if missing_unrelated:
            self.output("警告: 履歴上の無関係filter欠損: %d件" % missing_unrelated)

        phrase = "旧同期journalを完了として閉じる"
        if self.input("実行するには「%s」と入力: " % phrase) != phrase:
            return False
        confirmed, confirmed_bytes = journal_store.read_exact()
        confirmed_config, confirmed_sha256 = self._read_private_config()
        confirmed_automatic = self.expected_targets()
        confirmed_plan = self.plan_target_sync(confirmed_automatic)
        confirmed_snapshot = self._canonical_filter_snapshot(confirmed_plan["rules"])
        if confirmed != durable or confirmed_bytes != durable_bytes:
            raise RuntimeError("確認中に旧同期journalが変更されたため閉じませんでした")
        if confirmed_config != remote or confirmed_sha256 != remote_sha256:
            raise RuntimeError("確認中に秘密設定が変更されたため旧同期journalを閉じませんでした")
        if confirmed_automatic != automatic:
            raise RuntimeError("確認中に転送元が変更されたため旧同期journalを閉じませんでした")
        if confirmed_snapshot != initial_snapshot:
            raise RuntimeError("確認中にAPI filterが変更されたため旧同期journalを閉じませんでした")

        closed = json.loads(json.dumps(durable))
        closed["phase"] = "committed"
        closed["pending"] = None
        closed["retired_ids"] = [item["id"] for item in durable["old"]]
        written = journal_store.write(closed, expected=durable_bytes)
        readback, readback_bytes = journal_store.read_exact()
        if readback != closed or readback_bytes != written:
            raise RuntimeError("旧同期journalの完了readbackが一致しません")
        final_config, final_sha256 = self._read_private_config()
        final_automatic = self.expected_targets()
        final_plan = self.plan_target_sync(final_automatic)
        final_snapshot = self._canonical_filter_snapshot(final_plan["rules"])
        if (final_config != remote or final_sha256 != remote_sha256
                or final_automatic != automatic or final_snapshot != initial_snapshot):
            raise RuntimeError("旧同期journal完了後の外部変更を検出しました")
        self.config = dict(remote)
        return True

    def sync_targets(self, proposed_pinned=None):
        remote, remote_sha256 = self._read_private_config()
        if remote != self.config:
            self.output("競合: 起動後にリモート設定が変更されたため、再起動してやり直してください")
            return False
        journal_store = None
        migration_ftps = (getattr(self.release_workflow.deployer, "ftps", None)
                          if self.release_workflow is not None else None)
        journal_client = self.private_config_client
        if not (callable(getattr(journal_client, "read_scope_journal", None))
                and callable(getattr(
                    journal_client, "compare_and_swap_scope_journal", None))):
            journal_client = migration_ftps
        if (callable(getattr(journal_client, "read_scope_journal", None))
                and callable(getattr(
                    journal_client, "compare_and_swap_scope_journal", None))):
            journal_store = ScopeJournal(
                journal_client,
                self.release_workflow.PRIVATE_ROOT
                + "/deploy-transactions/target-sync-scope.json",
            )
        if journal_store is not None:
            durable, durable_bytes = journal_store.read_exact()
        else:
            durable, durable_bytes = None, None
        resuming_transaction = (durable is not None and durable["phase"] != "committed")
        if durable is not None and durable["phase"] != "committed" \
                and durable["schema_version"] != 2:
            if proposed_pinned is None and durable["schema_version"] == 1:
                return self._close_legacy_v1_target_journal(
                    remote=remote, remote_sha256=remote_sha256,
                    durable=durable, durable_bytes=durable_bytes,
                    journal_store=journal_store)
            raise RuntimeError("旧形式の未完了同期journalがあるため変更できません")
        if durable is not None and durable["phase"] != "committed":
            desired_pinned = list(durable["desired_pinned"])
            desired_targets = list(durable["desired_targets"])
        else:
            try:
                configured_pinned = canonical_email_list(
                    remote.get("notification_pinned_targets", []), allow_empty=True,
                    reject_duplicates=True,
                )
                desired_pinned = (configured_pinned if proposed_pinned is None else
                                  canonical_email_list(proposed_pinned, allow_empty=True))
                automatic = self.expected_targets()
                desired_targets = canonical_email_list(
                    automatic + desired_pinned, allow_empty=False)
            except CanonicalEmailError:
                raise RuntimeError("恒久通知対象設定が不正です") from None
        candidate_config = dict(remote)
        candidate_config["notification_pinned_targets"] = desired_pinned
        candidate_config["notification_targets"] = desired_targets
        if all(key in candidate_config for key in (
                "error_recipients", "notification_pinned_targets",
                "notification_targets", "system_mail_hmac_key", "log_path")):
            self._validate_runtime_config(candidate_config, strict=True)
        plan = self.plan_target_sync(desired_targets)
        self._print_target_sync_diff(plan)
        confirmation = "通知対象を同期する"
        if self.input("実行するには「%s」と入力: " % confirmation) != confirmation:
            return False
        confirmed, confirmed_sha256 = self._read_private_config()
        if confirmed != remote or confirmed_sha256 != remote_sha256:
            self.output("競合: 確認中にリモート設定が変更されたため、書き込みませんでした")
            return False

        stale_rules = plan["retire"]
        desired = [self._rule(address) for address in plan["add"]]
        if durable is not None and durable["phase"] != "committed":
            current_by_id = {rule["id"]: rule for rule in plan["rules"]}
            authorized = {
                item["id"]: (item["sha256"], "full") for item in durable["unrelated"]
            }
            authorized.update({
                item["id"]: (item["sha256"], "body")
                for item in durable["old"] + durable["new_ids"]
            })
            pending_add_hash = (durable["pending"]["sha256"]
                                if durable["pending"] is not None
                                and durable["pending"]["kind"] == "add" else None)
            unknown = []
            for rule_id, rule in current_by_id.items():
                expected = authorized.get(rule_id)
                if expected is None:
                    if (pending_add_hash is not None and ScopeJournal.digest(
                            ScopeJournal.body(rule)) == pending_add_hash):
                        unknown.append(rule_id)
                        continue
                    raise RuntimeError("同期API baselineが一致しません")
                actual = (ScopeJournal.digest(rule) if expected[1] == "full" else
                          ScopeJournal.digest(ScopeJournal.body(rule)))
                if actual != expected[0]:
                    raise RuntimeError("同期API baselineが一致しません")
            if len(unknown) > 1:
                raise RuntimeError("同期API baselineが一致しません")
            configured = remote.get("notification_targets")
            if not isinstance(configured, list):
                raise RuntimeError("通知対象設定を確認できません")
            candidate_addresses = sorted(
                set(configured) | set(durable["desired_targets"]) | set(plan["delete"]),
                key=lambda item: item.encode("ascii"),
            )
            candidate_new = {ScopeJournal.digest(ScopeJournal.body(self._rule(address))):
                             self._rule(address) for address in candidate_addresses}
            desired = [candidate_new[item] for item in durable["new"] if item in candidate_new]
            if len(desired) != len(durable["new"]):
                raise RuntimeError("同期journalの追加対象が一致しません")
            old_by_hash = {}
            for address in candidate_addresses:
                for candidate in (self._rule(address), self._legacy_rule(address)):
                    old_by_hash[ScopeJournal.digest(ScopeJournal.body(candidate))] = candidate
            stale_rules = []
            for item in durable["old"]:
                candidate = old_by_hash.get(item["sha256"])
                if candidate is None:
                    raise RuntimeError("同期journalの削除対象が一致しません")
                stale_rules.append(dict(candidate, id=item["id"]))
            known_new = {item["id"] for item in durable["new_ids"]}
            plan["rules"] = ([rule for rule in plan["rules"]
                              if rule["id"] not in known_new
                              and (pending_add_hash is None or ScopeJournal.digest(
                                  ScopeJournal.body(rule)) != pending_add_hash)]
                             + [rule for rule in stale_rules
                                if not any(current["id"] == rule["id"]
                                           for current in plan["rules"])])
        elif durable is not None:
            # A completed transaction is an immutable audit record; a later sync starts fresh.
            durable = None
        if durable is None:
            if journal_store is not None:
                durable = journal_store.prepare_v2(
                    plan["rules"], stale_rules, desired,
                    desired_pinned=desired_pinned, desired_targets=desired_targets,
                    config_before_sha256=remote_sha256,
                    config_expected_sha256=ScopeJournal.digest(candidate_config),
                    replace_committed=True)
                prepared, durable_bytes = journal_store.read_exact()
                if prepared != durable:
                    raise RuntimeError("同期journalの作成readbackが一致しません")
            else:
                durable = None
        seed = {}
        if durable is not None:
            _ScopedMigrationApi(self.api, plan["rules"], stale_rules, desired,
                                journal=seed)
            seed["retired_ids"] = list(durable["retired_ids"])
            seed["new_ids"] = {item["id"]: item["sha256"] for item in durable["new_ids"]}
            pending = durable["pending"]
            seed["pending"] = (None if pending is None else {
                "op": pending["kind"], **({"id": pending["id"]}
                    if pending["kind"] == "delete" else {"body_sha256": pending["sha256"]})})
        def persist_scope(state):
            nonlocal durable_bytes
            if journal_store is None:
                return
            durable["phase"] = "active"
            durable["retired_ids"] = list(state["retired_ids"])
            durable["new_ids"] = [{"id": key, "sha256": value}
                                  for key, value in sorted(state["new_ids"].items())]
            pending_state = state["pending"]
            hashes = {item["id"]: item["sha256"]
                      for item in durable["old"] + durable["new_ids"]}
            durable["pending"] = (None if pending_state is None else {
                "kind": pending_state["op"], "id": pending_state.get("id", ""),
                "sha256": pending_state.get("body_sha256",
                                              hashes.get(pending_state.get("id"), ""))})
            durable_bytes = journal_store.write(durable, expected=durable_bytes)
        scoped = _ScopedMigrationApi(
            self.api, plan["rules"], stale_rules, desired,
            journal=seed if durable is not None else None, persist_journal=persist_scope,
        )
        recovered_rules = scoped.snapshot_filters()
        recovered_bodies = {_ScopedMigrationApi._body(rule) for rule in recovered_rules}
        for rule in desired:
            if _ScopedMigrationApi._body(rule) not in recovered_bodies:
                scoped.add_filter(rule)
        recovered_rules = scoped.snapshot_filters()
        recovered_ids = {rule["id"] for rule in recovered_rules}
        for rule in stale_rules:
            if rule["id"] in recovered_ids:
                scoped.delete_filter(rule["id"])
        scoped.snapshot_filters()

        latest, latest_sha256 = self._read_private_config()
        config_matches_intent = (
            latest.get("notification_pinned_targets") == desired_pinned
            and latest.get("notification_targets") == desired_targets
        )
        if durable is not None and durable["config_applied_sha256"] is not None:
            if (latest_sha256 != durable["config_applied_sha256"]
                    or ScopeJournal.digest(latest) != durable["config_expected_sha256"]):
                raise RuntimeError("通知対象設定のCAS証跡とreadbackが一致しません")
        elif config_matches_intent and durable is not None and resuming_transaction:
            if ScopeJournal.digest(latest) != durable["config_expected_sha256"]:
                raise RuntimeError("通知対象設定のCAS証跡を確認できません")
            durable["phase"] = "active"
            durable["config_applied_sha256"] = latest_sha256
            durable_bytes = journal_store.write(durable, expected=durable_bytes)
        elif durable is not None or not config_matches_intent:
            expected_before = (durable["config_before_sha256"]
                               if durable is not None else remote_sha256)
            if latest != remote or latest_sha256 != expected_before:
                raise RuntimeError("API同期中にリモート設定が変更されたため書き込みませんでした")
            updated = dict(latest)
            updated["notification_pinned_targets"] = desired_pinned
            updated["notification_targets"] = desired_targets
            if (durable is not None and ScopeJournal.digest(updated)
                    != durable["config_expected_sha256"]):
                raise RuntimeError("通知対象設定の完全な期待値が一致しません")
            if not self._replace_private_config(latest_sha256, updated):
                raise RuntimeError("API同期中にリモート設定が変更されたため書き込みませんでした")
            latest, latest_sha256 = self._read_private_config()
            if latest != updated:
                raise RuntimeError("通知対象設定のreadbackが一致しません")
            if durable is not None:
                durable["phase"] = "active"
                durable["config_applied_sha256"] = latest_sha256
                durable_bytes = journal_store.write(durable, expected=durable_bytes)
        if journal_store is not None:
            final_config, final_config_sha256 = self._read_private_config()
            if (durable["config_applied_sha256"] is None
                    or final_config_sha256 != durable["config_applied_sha256"]
                    or ScopeJournal.digest(final_config)
                       != durable["config_expected_sha256"]):
                raise RuntimeError("通知対象設定の最終CAS readbackが一致しません")
            latest = final_config
        final_plan = self.plan_target_sync(desired_targets)
        if final_plan["add"] or final_plan["delete"] \
                or latest.get("notification_pinned_targets") != desired_pinned \
                or latest.get("notification_targets") != desired_targets:
            raise RuntimeError("通知対象同期の最終readbackが一致しません")
        if journal_store is not None:
            self._verify_committable_scope(durable, final_plan["rules"])
            durable["phase"] = "committed"
            durable["pending"] = None
            durable_bytes = journal_store.write(durable, expected=durable_bytes)
        self.config = dict(latest)
        return True

    def list_targets(self):
        try:
            addresses = canonical_email_list(
                self.config.get("notification_pinned_targets", []), allow_empty=True,
                reject_duplicates=True,
            )
        except CanonicalEmailError:
            raise RuntimeError("恒久通知対象設定が不正です") from None
        self.output("恒久通知対象: " + (", ".join(addresses) if addresses else "（なし）"))

    def add_targets(self):
        additions = self._read_addresses("追加する恒久通知対象（カンマ区切り）: ")
        current = canonical_email_list(
            self.config.get("notification_pinned_targets", []), allow_empty=True,
            reject_duplicates=True,
        )
        return self.sync_targets(canonical_email_list(current + additions, allow_empty=True))

    def change_target(self):
        old = validate_email(self.input("変更前の恒久通知対象: "))
        new = validate_email(self.input("変更後の恒久通知対象: "))
        current = canonical_email_list(
            self.config.get("notification_pinned_targets", []), allow_empty=True,
            reject_duplicates=True,
        )
        if old not in current:
            raise ValueError("変更対象が見つかりません")
        return self.sync_targets(canonical_email_list(
            [new if item == old else item for item in current], allow_empty=True))

    def delete_target(self):
        address = validate_email(self.input("削除する恒久通知対象: "))
        current = canonical_email_list(
            self.config.get("notification_pinned_targets", []), allow_empty=True,
            reject_duplicates=True,
        )
        if address not in current:
            automatic = self.expected_targets()
            if address in automatic:
                raise ValueError("自動検出対象は恒久通知対象から削除できません")
            raise ValueError("削除対象が見つかりません")
        return self.sync_targets([item for item in current if item != address])

    def list_error_recipients(self):
        self.output("エラー通知先: " + (", ".join(self.error_recipients) if self.error_recipients else "（なし）"))

    def _read_error_config(self):
        config, config_sha256 = self._read_private_config()
        if all(key in config for key in ("notification_pinned_targets",
                                          "notification_targets",
                                          "system_mail_hmac_key", "log_path")):
            config = self._validate_runtime_config(config, strict=True)
            current = config["error_recipients"]
        else:
            try:
                current = canonical_email_list(
                    config.get("error_recipients"), allow_empty=False
                )
            except CanonicalEmailError:
                raise RuntimeError("リモート設定のerror_recipientsが不正です") from None
        return config, current, config_sha256

    def _deploy_error_recipients(self, proposed, confirmation, config, current, config_sha256):
        try:
            proposed = canonical_email_list(proposed, allow_empty=False)
        except CanonicalEmailError:
            raise ValueError("エラー通知先は1件以上必要です") from None
        for address in current:
            if address not in proposed:
                self.output("- " + address)
        for address in proposed:
            if address not in current:
                self.output("+ " + address)
        if config != self.config:
            self.output("競合: 起動後にリモート設定が変更されたため、再起動してやり直してください")
            return False
        if self.input("実行するには「%s」と入力: " % confirmation) != confirmation:
            return False
        latest, latest_sha256 = self._read_private_config()
        if latest != config or latest_sha256 != config_sha256:
            self.output("競合: 確認中にリモート設定が変更されたため、書き込みませんでした")
            return False
        config = dict(config)
        config["error_recipients"] = proposed
        if all(key in config for key in ("notification_pinned_targets",
                                          "notification_targets",
                                          "system_mail_hmac_key", "log_path")):
            config = self._validate_runtime_config(config, strict=True)
        if not self._replace_private_config(latest_sha256, config):
            self.output("競合: 確認中にリモート設定が変更されたため、書き込みませんでした")
            return False
        self.config = config
        self.error_recipients = proposed
        return True

    def add_error_recipients(self):
        additions = self._read_addresses("追加するエラー通知先（カンマ区切り）: ")
        config, current, config_sha256 = self._read_error_config()
        proposed = canonical_email_list(current + additions, allow_empty=False)
        return self._deploy_error_recipients(proposed, "配備する", config, current, config_sha256)

    def change_error_recipient(self):
        old = validate_email(self.input("変更前のエラー通知先: "))
        new = validate_email(self.input("変更後のエラー通知先: "))
        config, current, config_sha256 = self._read_error_config()
        if old not in current:
            raise ValueError("変更対象が見つかりません")
        proposed = [new if item == old else item for item in current]
        proposed = canonical_email_list(proposed, allow_empty=False)
        return self._deploy_error_recipients(proposed, "変更して配備する", config, current, config_sha256)

    def delete_error_recipient(self):
        address = validate_email(self.input("削除するエラー通知先: "))
        config, current, config_sha256 = self._read_error_config()
        if address not in current:
            raise ValueError("削除対象が見つかりません")
        proposed = [item for item in current if item != address]
        return self._deploy_error_recipients(proposed, "削除して配備する", config, current, config_sha256)

    def _default_diagnostics(self):
        rules = self._managed()
        config, _config_sha256 = self._read_private_config()
        api_targets = sorted(self._address(rule) for rule in rules)
        api_target_set = set(api_targets)
        configured_targets_value = config.get("notification_targets")
        targets_valid = (
            isinstance(configured_targets_value, list)
            and all(isinstance(target, str) for target in configured_targets_value)
        )
        configured_targets = sorted(configured_targets_value) if targets_valid else []
        configured_pinned_value = config.get("notification_pinned_targets")
        pinned_valid = (
            isinstance(configured_pinned_value, list)
            and all(isinstance(target, str) for target in configured_pinned_value)
        )
        configured_pinned = sorted(configured_pinned_value) if pinned_valid else []
        pinned_registered = sum(target in api_target_set for target in configured_pinned)
        pinned_synced = (
            pinned_valid
            and len(set(configured_pinned)) == len(configured_pinned)
            and pinned_registered == len(configured_pinned)
        )
        targets_synced = targets_valid and api_targets == configured_targets
        command_ok = all(rule.get("action", {}).get("target", self.command_target) == self.command_target for rule in rules)
        configured_command = config.get("command_path")
        synced = (
            targets_synced
            and pinned_synced
            and isinstance(configured_command, str)
            and command_ok
            and configured_command == self.command_path
        )
        try:
            health_summary = self._require_private_config_client().health_summary()
            health_state = health_summary.get("state") if type(health_summary) is dict else None
            health = {
                "missing": "未作成",
                "healthy": "正常",
                "degraded": "障害中",
            }.get(health_state, "状態ファイル不正")
        except Exception:
            health = "状態ファイル不正"
        release_configured = any(
            type(config.get(key)) is str and bool(config[key])
            for key in ("release_path", "release_id")
        )
        return {
            "sync": "%s（API %d件 / リモート設定 %d件）" % ("一致" if synced else "不一致", len(api_targets), len(configured_targets)),
            "pinned": "%s（API登録 %d/%d件）" % (
                "一致" if pinned_synced else "不一致",
                pinned_registered, len(configured_pinned),
            ),
            "targets": "%s（API %d件 / リモート設定 %d件）" % (
                "一致" if targets_synced else "不一致",
                len(api_targets), len(configured_targets),
            ),
            "health": health,
            "latest_log": "リモートリリース: %s" % (
                "設定済み" if release_configured else "不明"
            ),
        }

    def _default_lineworks_test(self):
        config, _config_sha256 = self._read_private_config()
        webhook_url = config.get("webhook_url")
        try:
            validate_webhook_url(webhook_url)
        except RuntimeError:
            raise RuntimeError("リモート設定のWebhook URLが不正です")
        payload = {"title": "LINE WORKS接続テスト", "body": {"text": "管理CLIからのテスト通知です"}}
        request = Request(
            webhook_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST",
        )
        try:
            with urlopen(request, timeout=15, context=_build_verified_ssl_context()) as response:
                status = getattr(response, "status", 200)
                response.read()
        except Exception as error:
            raise RuntimeError("LINE WORKSテスト送信に失敗しました") from error
        if not 200 <= status < 300:
            raise RuntimeError("LINE WORKSテスト送信に失敗しました（HTTP %d）" % status)

    def _require_private_config_client(self):
        if self.private_config_client is None:
            raise RuntimeError("秘密設定のSSH接続を利用できません。")
        return self.private_config_client

    def _read_private_config(self):
        try:
            return self._require_private_config_client().read()
        except Exception:
            raise RuntimeError("秘密設定を安全に読み取れませんでした。") from None

    def _replace_private_config(self, expected_sha256, updated):
        client = self._require_private_config_client()
        result = client.compare_and_swap(expected_sha256, updated)
        if result.status == "conflict":
            return False
        if result.status not in {"changed", "unchanged"}:
            raise RuntimeError("秘密設定の変更を確認できませんでした。")
        readback, readback_sha256 = client.read()
        if readback_sha256 != result.new_sha256 or readback != updated:
            raise RuntimeError("秘密設定の変更を確認できませんでした。")
        return True

    def show_webhook_url(self):
        try:
            config, _sha256 = self._require_private_config_client().read()
            webhook_url = validate_webhook_url(config.get("webhook_url"))
        except Exception:
            raise RuntimeError("Webhook URLを安全に読み取れませんでした。") from None
        self.output("Webhook URL: " + mask_webhook_url(webhook_url))
        phrase = "Webhook URLを表示する"
        if self.input("全文を一度だけ表示するには「%s」と入力: " % phrase) == phrase:
            self.output(webhook_url)
        return True

    def change_webhook_url(self):
        client = self._require_private_config_client()
        try:
            current, expected_sha256 = client.read()
        except Exception:
            raise RuntimeError("Webhook URL変更用の秘密設定を読み取れませんでした。") from None
        entered = self.input("新しいWebhook URL: ")
        try:
            new_url = validate_webhook_url(entered)
        except RuntimeError:
            raise RuntimeError("Webhook URLの形式が正しくありません。") from None
        self.output("変更先: " + mask_webhook_url(new_url))
        phrase = "Webhook URLを変更する"
        if self.input("接続確認して変更するには「%s」と入力: " % phrase) != phrase:
            return False
        payload = {
            "title": "Webhook URL変更テスト",
            "body": {"text": "Mac管理アプリからの接続確認です。"},
        }
        try:
            status = self.webhook_sender(new_url, payload)
        except Exception:
            raise RuntimeError("Webhook接続確認に失敗したため変更しませんでした。") from None
        if type(status) is not int or status != 200:
            raise RuntimeError("Webhook接続確認がHTTP 200ではないため変更しませんでした。")
        updated = dict(current)
        updated["webhook_url"] = new_url
        try:
            result = client.compare_and_swap(expected_sha256, updated)
        except Exception:
            raise RuntimeError("秘密設定の変更を確認できませんでした。") from None
        if result.status == "conflict":
            raise RuntimeError("秘密設定が競合しました。最新状態を読み直して再試行してください。")
        if result.status == "restored":
            raise RuntimeError("秘密設定の変更に失敗し、以前の状態へ復元されました。")
        if result.status not in {"changed", "unchanged"}:
            raise RuntimeError("秘密設定の変更結果を確認できませんでした。")
        try:
            readback, readback_sha256 = client.read()
        except Exception:
            raise RuntimeError("変更後の秘密設定を読み取れませんでした。") from None
        if (readback_sha256 != result.new_sha256
                or readback.get("webhook_url") != new_url):
            raise RuntimeError("変更後の秘密設定が一致しませんでした。")
        self.output("Webhook URLの変更を確認しました。")
        return True

    def _default_error_mail_test(self):
        self.output("確認後10分間、指定件名の通知対象テストメールだけWebhook失敗を発生させ、サーバーのErrorReporter/sendmail経路を確認します")
        confirmation = "エラーメールをテストする"
        if self.input("実行するには「%s」と入力: " % confirmation) != confirmation:
            return False
        config, config_sha256 = self._read_private_config()
        token = self.test_token_fn()
        expires_at = self.now_fn().astimezone(timezone.utc) + timedelta(minutes=10)
        config.pop("force_webhook_failure_once", None)
        config["test_force_webhook_failure_until"] = expires_at.isoformat(timespec="seconds")
        config["test_error_subject_token"] = token
        validated = self._validate_runtime_config(config, strict=True)
        if not self._replace_private_config(config_sha256, validated):
            raise RuntimeError("秘密設定が競合したためテストを開始できませんでした")
        self.config = validated
        self.output("準備完了: 10分以内に件名を「[Error Test %s]」に完全一致させた通知対象テストメールを1通送信し、エラー通知先への到着を確認してください" % token)
        return True

    def show_diagnostics(self):
        result = self.diagnostic_fn()
        self.output("同期状態: " + str(result.get("sync", "不明")))
        self.output("恒久対象: " + str(result.get("pinned", "不明")))
        self.output("通知対象: " + str(result.get("targets", "不明")))
        self.output("配信状態: " + str(result.get("health", "不明")))
        self.output("最新ログ: " + str(result.get("latest_log", "なし")))

    def send_lineworks_test(self):
        self.lineworks_test_fn()
        self.output("LINE WORKSテストを実行しました")

    def send_error_mail_test(self):
        if self.error_mail_test_fn() is False:
            return False
        self.output("エラーメールテストを実行しました")
        return True

    def deploy_release(self):
        """Stage first; mutate locator/API only after separate exact confirmations."""
        if self.release_workflow is None:
            raise RuntimeError("リリース配備機能を開始できません")
        source = self.input("配備するローカルreleaseディレクトリの絶対パス: ").strip()
        release_id = self.input("リリースID（release-で開始）: ").strip()
        self.output("検証配備: " + release_id)
        if self.input("実行するには「検証配備する」と入力: ") != "検証配備する":
            return False
        if (self.private_config_client is not None
                and (Path(source) / "src/CanonicalEmail.php").is_file()):
            self.ensure_runtime_config()
        staged = self.release_workflow.stage(source, release_id)
        self.output("FTPS readbackとSSH検証が完了しました。APIとlocatorは未変更です。")
        validation = staged.get("validation", {}) if isinstance(staged, dict) else {}
        skipped_subtrees = validation.get("untrusted_subtrees", 0)
        skipped_entries = validation.get("untrusted_entries", 0)
        if ((type(skipped_subtrees) is int and skipped_subtrees > 0)
                or (type(skipped_entries) is int and skipped_entries > 0)):
            self.output(
                "警告: 公開領域で内容を確認せずスキップしました。"
                f"信頼できないサブツリー: {skipped_subtrees}、"
                f"信頼できない項目: {skipped_entries}"
            )
        mode = self.input("通常更新は「切替える」、初回filter移行は「初回移行する」、中止はEnter: ")
        if mode == "切替える":
            self.release_workflow.switch(staged)
            self.output("active releaseを切り替えました。")
            return True
        if mode != "初回移行する":
            return False
        targets = self.config.get("notification_targets")
        if not isinstance(targets, list) or not targets:
            raise RuntimeError("初回移行対象をリモート設定から確認できません")
        try:
            canonical_targets = [validate_email(address) for address in targets]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("初回移行対象をリモート設定から確認できません") from exc
        if canonical_targets != targets or len(canonical_targets) != len(set(canonical_targets)):
            raise RuntimeError("初回移行対象をリモート設定から一意に確認できません")
        targets = canonical_targets
        if not self.initial_command_targets:
            raise RuntimeError("初回移行前のコマンドを確認できません")
        current = self.api.snapshot_filters()
        ids = [rule.get("id") for rule in current]
        if (any(not isinstance(rule_id, str) or not rule_id for rule_id in ids)
                or len(ids) != len(set(ids))):
            raise RuntimeError("初回移行対象のAPI IDを一意に確認できません")
        desired = [self._rule(address) for address in targets]
        migration_ftps = getattr(self.release_workflow.deployer, "ftps", None)
        journal_client = self.private_config_client
        if not (callable(getattr(journal_client, "read_scope_journal", None))
                and callable(getattr(journal_client, "write_scope_journal", None))):
            journal_client = migration_ftps
        journal_store = (ScopeJournal(
            journal_client,
            self.release_workflow.PRIVATE_ROOT + "/deploy-transactions/filter-scope.json",
        ) if (callable(getattr(journal_client, "read_scope_journal", None))
              and callable(getattr(journal_client, "write_scope_journal", None))) else None)
        durable = journal_store.read() if journal_store is not None else None
        if durable is not None:
            if sorted(durable["new"]) != sorted(
                    ScopeJournal.digest(ScopeJournal.body(item)) for item in desired):
                raise RuntimeError("移行journalの新filter baselineが一致しません")
            by_id = {rule["id"]: rule for rule in current}
            for item in durable["unrelated"]:
                rule = by_id.get(item["id"])
                if rule is None or ScopeJournal.digest(rule) != item["sha256"]:
                    raise RuntimeError("移行対象外のfilter baselineが一致しません")
            known = ({item["id"] for item in durable["old"]}
                     | {item["id"] for item in durable["unrelated"]}
                     | {item["id"] for item in durable["new_ids"]})
            unknown = [rule for rule in current if rule["id"] not in known]
            pending = durable["pending"]
            if unknown:
                if (pending is None or pending["kind"] != "add" or len(unknown) != 1
                        or ScopeJournal.digest(ScopeJournal.body(unknown[0])) != pending["sha256"]):
                    raise RuntimeError("認可されていないfilter追加を検出しました")
                durable["new_ids"].append({"id": unknown[0]["id"], "sha256": pending["sha256"]})
                durable["pending"] = None
            journal_store.write(durable)
        old_rules = []
        for address in targets:
            candidates = [{
                "domain": address.rsplit("@", 1)[1],
                "conditions": [{"field": "to", "match_type": "match", "keyword": address}],
                "action": {"type": "mail_address", "target": target, "method": "copy"},
            } for target in self.initial_command_targets]
            if durable is None:
                matches = [rule for rule in current if any(
                    ScopeJournal.body(rule) == ScopeJournal.body(candidate)
                    for candidate in candidates
                )]
                if len(matches) != 1:
                    raise RuntimeError("初回移行対象をAPIから一意に確認できません")
                old_rules.append(matches[0])
                continue
            candidates_by_hash = {
                ScopeJournal.digest(ScopeJournal.body(candidate)): candidate
                for candidate in candidates
            }
            journal_matches = [item for item in durable["old"]
                               if item["sha256"] in candidates_by_hash]
            if len(journal_matches) != 1:
                raise RuntimeError("移行journalの旧filter baselineが一致しません")
            expected_hash = journal_matches[0]["sha256"]
            expected = candidates_by_hash[expected_hash]
            journal_id = journal_matches[0]["id"]
            present = [rule for rule in current if rule["id"] == journal_id]
            if present:
                if len(present) != 1 or ScopeJournal.body(present[0]) != ScopeJournal.body(expected):
                    raise RuntimeError("移行journalの旧filter IDまたはbodyが一致しません")
                old_rules.append(present[0])
                continue
            pending_delete = (durable["pending"] is not None
                              and durable["pending"]["kind"] == "delete"
                              and durable["pending"]["id"] == journal_id
                              and durable["pending"]["sha256"] == expected_hash)
            if journal_id not in durable["retired_ids"] and not pending_delete:
                raise RuntimeError("認可されていない旧filter欠落を検出しました")
            old_rules.append(dict(expected, id=journal_id))
        if len({rule["id"] for rule in old_rules}) != len(targets):
            raise RuntimeError("初回移行対象をAPIから一意に確認できません")
        if durable is None:
            if journal_store is not None:
                durable = journal_store.prepare(current, old_rules, desired)
            else:
                old_ids_for_state = {item["id"] for item in old_rules}
                durable = {"schema_version": 1, "phase": "prepared", "pending": None,
                    "old": [{"id": item["id"], "sha256": ScopeJournal.digest(ScopeJournal.body(item))}
                            for item in old_rules],
                    "new": [ScopeJournal.digest(ScopeJournal.body(item)) for item in desired],
                    "unrelated": [{"id": item["id"], "sha256": ScopeJournal.digest(item)}
                                  for item in current if item["id"] not in old_ids_for_state],
                    "new_ids": [], "retired_ids": []}
        old_ids = {item["id"] for item in durable["old"]}
        new_ids = {item["id"] for item in durable["new_ids"]}
        baseline = old_rules + [rule for rule in current
            if rule["id"] not in old_ids and rule["id"] not in new_ids]
        warning = (
            "移行中はLINE WORKS通知が停止する可能性があります。メール原本はmailboxへ残ります。"
        )
        self.output(warning)
        confirmation = "通知停止とメールボックス確認を了承します"
        deployer = self.release_workflow.deployer
        missing_api = object()
        original_api = getattr(deployer, "api", missing_api)
        seed = {}
        _ScopedMigrationApi(self.api, baseline, old_rules, desired, journal=seed)
        seed["retired_ids"] = list(durable["retired_ids"])
        seed["new_ids"] = {item["id"]: item["sha256"] for item in durable["new_ids"]}
        pending = durable["pending"]
        seed["pending"] = (None if pending is None else {
            "op": pending["kind"], **({"id": pending["id"]} if pending["kind"] == "delete"
                                     else {"body_sha256": pending["sha256"]})
        })
        def persist_scope(state):
            pending_state = state["pending"]
            durable["phase"] = "active"
            durable["retired_ids"] = list(state["retired_ids"])
            durable["new_ids"] = [{"id": key, "sha256": value}
                                  for key, value in sorted(state["new_ids"].items())]
            durable["pending"] = (None if pending_state is None else {
                "kind": pending_state["op"],
                "id": pending_state.get("id", ""),
                "sha256": pending_state.get("body_sha256", next(
                    (item["sha256"] for item in durable["old"] + durable["new_ids"]
                     if item["id"] == pending_state.get("id")), ""
                )),
            })
            if journal_store is not None:
                journal_store.write(durable)
        scoped_api = _ScopedMigrationApi(
            self.api, baseline, old_rules, desired, journal=seed,
            persist_journal=persist_scope,
        )
        deployer.api = scoped_api
        try:
            deployer.bootstrap_migrate(
                old_rules, desired,
                state_path=self.release_workflow.PRIVATE_ROOT + "/deploy-transactions/filter-migration.json",
                maintenance_marker_path=self.release_workflow.PRIVATE_ROOT + "/deploy-transactions/filter-maintenance.json",
                max_downtime_seconds=120,
                confirm_maintenance=lambda _warning: (
                    confirmation if self.input(
                    "実行するには「%s」と入力: " % confirmation
                    ) == confirmation else ""
                ),
                switch_pair=lambda: (
                    scoped_api.snapshot_filters(), self.release_workflow.switch(staged)
                )[1],
                output=self.output,
            )
            durable["phase"] = "committed"
            durable["pending"] = None
            if journal_store is not None:
                journal_store.write(durable)
        finally:
            if original_api is missing_api:
                del deployer.api
            else:
                deployer.api = original_api
        release_path = staged.get("release_path") if isinstance(staged, dict) else None
        release_id = staged.get("release_id") if isinstance(staged, dict) else None
        if isinstance(release_path, str) and release_path and isinstance(release_id, str) and release_id:
            latest_config, latest_sha256 = self._read_private_config()
            if latest_config != self.config:
                raise RuntimeError("移行中にリモート設定が変更されたため同期できません")
            updated_config = dict(latest_config)
            updated_config["command_path"] = self.command_path
            updated_config["release_path"] = release_path
            updated_config["release_id"] = release_id
            if not self._replace_private_config(latest_sha256, updated_config):
                raise RuntimeError("移行中にリモート設定が変更されたため同期できません")
            self.config = updated_config
        self.output("初回filter移行が完了しました。メール原本の消失はありません。")
        return True

    def run(self):
        actions = {
            "1": self.list_targets, "2": self.add_targets,
            "3": self.change_target, "4": self.delete_target,
            "5": self.list_error_recipients,
            "6": self.add_error_recipients, "7": self.change_error_recipient,
            "8": self.delete_error_recipient, "9": self.show_diagnostics,
            "10": self.send_lineworks_test, "11": self.send_error_mail_test,
            "12": self.deploy_release, "13": self.sync_targets,
            "14": self.show_webhook_url, "15": self.change_webhook_url,
        }
        while True:
            self.output(self.MENU)
            choice = self.input("番号を選択: ").strip()
            if choice == "0":
                return
            action = actions.get(choice)
            if action is None:
                self.output("番号が正しくありません")
                continue
            action()


def _run_main():
    """Build clients from non-secret environment metadata and macOS Keychain."""
    try:
        from manager.ftps_deployer import FtpsDeployer
        from manager.keychain import Keychain
        from manager.xserver_api import XServerApi
        from manager.remote_validator import RemoteValidator
        from manager.release_deployer import ReleaseDeployer
        from manager.release_workflow import ReleaseWorkflow
        from manager.private_config_ssh import PrivateConfigSsh
    except ModuleNotFoundError:  # Support `python3 manager/manage.py` from the repository root.
        from ftps_deployer import FtpsDeployer
        from keychain import Keychain
        from xserver_api import XServerApi
        from remote_validator import RemoteValidator
        from release_deployer import ReleaseDeployer
        from release_workflow import ReleaseWorkflow
        from private_config_ssh import PrivateConfigSsh

    required = (
        "XSERVER_SERVERNAME", "XSERVER_COMMAND_PATH", "XSERVER_FTPS_HOST",
        "XSERVER_CONFIG_PATH", "XSERVER_HOME", "XSERVER_SSH_ALIAS",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("環境設定が不足しています: " + ", ".join(missing), file=sys.stderr)
        return 2
    keychain = Keychain(ftps_host=os.environ["XSERVER_FTPS_HOST"])
    username, password = keychain.read_ftps_credentials()
    stable_command_path = (
        os.environ["XSERVER_HOME"].rstrip("/")
        + "/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php"
    )
    wrapper_command_path = (
        os.environ["XSERVER_HOME"].rstrip("/")
        + "/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php"
    )
    api = XServerApi(os.environ["XSERVER_SERVERNAME"], keychain.read_api_key(), wrapper_command_path)
    deployer = FtpsDeployer(
        os.environ["XSERVER_FTPS_HOST"], username, password,
        config_remote_path=os.environ["XSERVER_CONFIG_PATH"],
        filesystem_home=os.environ["XSERVER_HOME"],
    )
    validator = RemoteValidator(os.environ["XSERVER_SSH_ALIAS"])
    filesystem_config_path = _filesystem_config_path(
        os.environ["XSERVER_HOME"], os.environ["XSERVER_CONFIG_PATH"]
    )
    release_deployer = ReleaseDeployer(
        deployer, validator, api,
        validation_context={
            "entrypoint": ReleaseWorkflow.ENTRYPOINT,
            "server_home": os.environ["XSERVER_HOME"],
            "config_path": filesystem_config_path,
            "expected_hosts": [os.environ["XSERVER_FTPS_HOST"], os.environ["XSERVER_SERVERNAME"]],
            "known_basenames": ["xserver-mail-lineworks"],
        },
    )
    release_workflow = ReleaseWorkflow(
        release_deployer, os.environ["XSERVER_HOME"],
        filesystem_config_path,
    )
    expected_hosts = [os.environ["XSERVER_FTPS_HOST"], os.environ["XSERVER_SERVERNAME"]]
    private_config_client = PrivateConfigSsh(
        os.environ["XSERVER_SSH_ALIAS"], os.environ["XSERVER_HOME"],
        expected_hosts=expected_hosts,
    )
    try:
        try:
            config, _config_sha256 = private_config_client.read()
        except (RuntimeError, ValueError, OSError):
            phrase = "秘密設定helperを初期配備する"
            if input("legacy環境を検証してhelperだけを配備するには「%s」と入力: " % phrase) != phrase:
                raise RuntimeError("bootstrap was not confirmed") from None
            helper_asset, manifest_asset, asset_mode = _legacy_bootstrap_asset_paths()
            release_workflow.provision_legacy_helper_assets(
                helper_asset, manifest_asset, expected_mode=asset_mode)
            config, _config_sha256 = private_config_client.read()
        recipients = config.get("error_recipients", [])
        if not isinstance(recipients, list) or not recipients:
            raise RuntimeError("error_recipients must be a non-empty list")
        recipients = [validate_email(address) for address in recipients]
    except (RuntimeError, ValueError):
        print("リモート設定を読み込めないため、変更を禁止します。", file=sys.stderr)
        return 2
    MailManager(
        api, deployer, wrapper_command_path,
        config=config, error_recipients=recipients, release_workflow=release_workflow,
        initial_command_path=stable_command_path, private_config_client=private_config_client,
    ).run()
    return 0


def main():
    """Run the management CLI without exposing exception details or secrets."""
    try:
        return _run_main()
    except (RuntimeError, ValueError, OSError):
        print(
            "認証情報または接続設定を確認できません。キーチェーンと設定を確認してください。",
            file=sys.stderr,
        )
        return 2
    except Exception:
        print("予期しないエラーで管理CLIを開始できませんでした。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
