"""Secret-preserving SSH client for the fixed private-config helper."""

from __future__ import annotations

import json
import base64
import binascii
import hashlib
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from manager.remote_validator import RemoteValidator, bounded_subprocess_run
except ModuleNotFoundError:  # Bundled direct execution from the manager directory.
    from remote_validator import RemoteValidator, bounded_subprocess_run


_HOME = re.compile(r"/home/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_REQUEST_LIMIT = 262144
_RESPONSE_LIMIT = 131072
_HEALTH_CLASSIFICATIONS = {
    "success", "invalid_payload", "invalid_parameter", "missing_parameter",
    "invalid_webhook_url", "rate_limited", "http_error", "transport_error",
    "forced_test_failure", "internal_error", "system_mail_suppressed",
    "health_state_failure", "unknown",
}
_SCOPE_JOURNAL_LIMIT = 65536


@dataclass(frozen=True)
class ConfigCasResult:
    status: str
    old_sha256: str
    new_sha256: str


class PrivateConfigSsh:
    def __init__(self, ssh_alias: str, filesystem_home: str, *, expected_hosts: list[str],
                 runner=bounded_subprocess_run):
        if type(filesystem_home) is not str or _HOME.fullmatch(filesystem_home) is None:
            raise ValueError("filesystem_home is invalid")
        if (type(expected_hosts) is not list or not expected_hosts
                or len(set(expected_hosts)) != len(expected_hosts)
                or any(type(host) is not str or _HOST.fullmatch(host) is None
                       for host in expected_hosts)):
            raise ValueError("expected_hosts is invalid")
        helper = filesystem_home + "/private/xserver-mail-lineworks/bootstrap/manage-private-config.php"
        self.remote_command = "/usr/bin/php8.5 " + shlex.quote(helper)
        self.expected_hosts = list(expected_hosts)
        self.validator = RemoteValidator(ssh_alias, runner)

    @staticmethod
    def _unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate")
            value[key] = item
        return value

    def _request(self, request):
        payload = json.dumps(request, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        if len(payload) > _REQUEST_LIMIT:
            raise ValueError("設定要求が大きすぎます。")
        output = self.validator.run_trusted(
            self.remote_command, payload, expected_hosts=self.expected_hosts,
            output_limit=_RESPONSE_LIMIT,
        )
        if not isinstance(output, bytes):
            raise RuntimeError("秘密設定応答を確認できません。") from None
        invalid = False
        try:
            result = json.loads(output.decode("utf-8"), object_pairs_hook=self._unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
            invalid = True
            result = None
        if invalid:
            raise RuntimeError("秘密設定応答を確認できません。") from None
        return result

    def read(self) -> tuple[dict, str]:
        result = self._request({"schema_version": 1, "operation": "read"})
        if (type(result) is not dict or set(result) != {"schema_version", "config", "sha256"}
                or type(result.get("schema_version")) is not int
                or result.get("schema_version") != 1 or type(result.get("config")) is not dict
                or type(result.get("sha256")) is not str
                or _HASH.fullmatch(result["sha256"]) is None):
            raise RuntimeError("秘密設定応答を確認できません。")
        return result["config"], result["sha256"]

    def compare_and_swap(self, expected_sha256: str, updated: dict) -> ConfigCasResult:
        if (type(expected_sha256) is not str or _HASH.fullmatch(expected_sha256) is None
                or type(updated) is not dict):
            raise ValueError("秘密設定更新要求が不正です。")
        result = self._request({
            "schema_version": 1, "operation": "compare_and_swap",
            "expected_sha256": expected_sha256, "config": updated,
        })
        keys = {"schema_version", "status", "old_sha256", "new_sha256"}
        if (type(result) is not dict or set(result) != keys
                or type(result.get("schema_version")) is not int or result.get("schema_version") != 1
                or result.get("status") not in {"changed", "unchanged", "conflict", "restored"}
                or any(type(result.get(key)) is not str or _HASH.fullmatch(result[key]) is None
                       for key in ("old_sha256", "new_sha256"))):
            raise RuntimeError("秘密設定応答を確認できません。")
        return ConfigCasResult(result["status"], result["old_sha256"], result["new_sha256"])

    @staticmethod
    def _scope_journal_kind(kind: str) -> str:
        if type(kind) is not str or kind not in {"target-sync", "filter"}:
            raise ValueError("同期journal種別が不正です。")
        return kind

    def read_scope_journal(self, kind: str) -> bytes | None:
        result = self._request({
            "schema_version": 1, "operation": "scope-journal-read",
            "journal": self._scope_journal_kind(kind),
        })
        if (type(result) is not dict or result.get("schema_version") != 1
                or result.get("state") not in {"missing", "present"}):
            raise RuntimeError("秘密設定応答を確認できません。")
        if result["state"] == "missing":
            if set(result) != {"schema_version", "state"}:
                raise RuntimeError("秘密設定応答を確認できません。")
            return None
        if set(result) != {"schema_version", "state", "body_base64"} \
                or type(result.get("body_base64")) is not str:
            raise RuntimeError("秘密設定応答を確認できません。")
        try:
            encoded = result["body_base64"].encode("ascii")
            body = base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError):
            raise RuntimeError("秘密設定応答を確認できません。") from None
        if (not body or len(body) > _SCOPE_JOURNAL_LIMIT
                or base64.b64encode(body) != encoded):
            raise RuntimeError("秘密設定応答を確認できません。")
        return body

    def write_scope_journal(self, kind: str, body: bytes) -> bytes:
        kind = self._scope_journal_kind(kind)
        if kind == "target-sync":
            raise ValueError("同期journalはCAS更新が必要です。")
        if type(body) is not bytes or not body or len(body) > _SCOPE_JOURNAL_LIMIT:
            raise ValueError("同期journal更新要求が不正です。")
        result = self._request({
            "schema_version": 1, "operation": "scope-journal-write",
            "journal": kind, "body_base64": base64.b64encode(body).decode("ascii"),
        })
        expected = hashlib.sha256(body).hexdigest()
        if (type(result) is not dict or set(result) != {"schema_version", "sha256"}
                or result.get("schema_version") != 1
                or type(result.get("sha256")) is not str
                or not __import__("hmac").compare_digest(result["sha256"], expected)):
            raise RuntimeError("秘密設定応答を確認できません。")
        return body

    def compare_and_swap_scope_journal(
            self, kind: str, expected: bytes | None, desired: bytes) -> bytes:
        kind = self._scope_journal_kind(kind)
        if (kind != "target-sync"
                or (expected is not None and (type(expected) is not bytes
                    or not expected or len(expected) > _SCOPE_JOURNAL_LIMIT))
                or type(desired) is not bytes or not desired
                or len(desired) > _SCOPE_JOURNAL_LIMIT):
            raise ValueError("同期journal CAS要求が不正です。")
        expected_value = ({"state": "missing"} if expected is None else {
            "state": "present",
            "body_base64": base64.b64encode(expected).decode("ascii"),
            "sha256": hashlib.sha256(expected).hexdigest(),
        })
        request = {
            "schema_version": 1, "operation": "scope-journal-compare-and-swap",
            "journal": kind, "expected": expected_value,
            "desired": {
                "body_base64": base64.b64encode(desired).decode("ascii"),
                "sha256": hashlib.sha256(desired).hexdigest(),
            },
        }
        try:
            result = self._request(request)
            if (type(result) is not dict
                    or set(result) != {"schema_version", "status"}
                    or type(result.get("schema_version")) is not int
                    or result["schema_version"] != 1
                    or result.get("status") not in {
                        "changed", "already_applied", "conflict"}):
                raise RuntimeError
            if result["status"] in {"changed", "already_applied"}:
                return desired
        except Exception:
            pass
        try:
            current = self.read_scope_journal(kind)
        except Exception:
            raise RuntimeError("同期journal CAS結果が不明です。") from None
        if current == desired:
            return desired
        if current == expected:
            raise RuntimeError("同期journal CASは適用されませんでした。") from None
        raise RuntimeError("同期journal CAS結果が不明です。") from None

    def health_summary(self) -> dict:
        """Read only the fixed, redacted delivery-health summary from the helper."""
        result = self._request({"schema_version": 1, "operation": "health-summary"})
        keys = {
            "schema_version", "state", "changed_at", "classification",
            "next_observation_sequence", "last_applied_sequence",
        }
        valid = type(result) is dict and set(result) == keys
        if valid:
            state = result.get("state")
            changed_at = result.get("changed_at")
            classification = result.get("classification")
            next_sequence = result.get("next_observation_sequence")
            applied_sequence = result.get("last_applied_sequence")
            valid = (
                type(result.get("schema_version")) is int
                and result["schema_version"] == 1
                and type(state) is str and state in {"missing", "healthy", "degraded"}
                and (classification is None or (
                    type(classification) is str
                    and classification in _HEALTH_CLASSIFICATIONS
                ))
                and type(next_sequence) is int and next_sequence >= 0
                and type(applied_sequence) is int and applied_sequence >= 0
                and applied_sequence <= next_sequence
            )
            if changed_at is not None:
                valid = valid and type(changed_at) is str
                if valid:
                    try:
                        parsed = datetime.strptime(changed_at, "%Y-%m-%dT%H:%M:%SZ")
                        valid = parsed.replace(tzinfo=timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ) == changed_at
                    except (ValueError, OverflowError):
                        valid = False
            valid = valid and (
                (state == "missing" and changed_at is None and classification is None
                 and next_sequence == 0 and applied_sequence == 0)
                or (state == "healthy" and changed_at is not None
                    and classification == "success")
                or (state == "degraded" and changed_at is not None
                    and type(classification) is str
                    and classification in _HEALTH_CLASSIFICATIONS - {"success"})
            )
        if not valid:
            raise RuntimeError("秘密設定応答を確認できません。")
        return {key: result[key] for key in (
            "state", "changed_at", "classification", "next_observation_sequence",
            "last_applied_sequence",
        )}
