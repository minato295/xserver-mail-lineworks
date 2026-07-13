"""Durable, secret-free authorization journal for filter migration recovery."""

import hashlib
import json
import re

try:
    from manager.email_address import CanonicalEmailError, canonical_email_list
except ModuleNotFoundError:
    from email_address import CanonicalEmailError, canonical_email_list


class ScopeJournalError(RuntimeError):
    pass


class ScopeJournal:
    LIMIT = 65536
    V1_KEYS = {"schema_version", "phase", "old", "new", "unrelated", "pending",
               "new_ids", "retired_ids"}
    V2_KEYS = V1_KEYS | {
        "desired_pinned", "desired_targets", "config_before_sha256",
        "config_expected_sha256", "config_applied_sha256",
    }

    def __init__(self, ftps, path):
        self.ftps, self.path = ftps, path
        basename = path.rsplit("/", 1)[-1]
        kinds = {"target-sync-scope.json": "target-sync", "filter-scope.json": "filter"}
        if basename not in kinds:
            raise ValueError("scope journal path is invalid")
        self.kind = kinds[basename]

    @staticmethod
    def _unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate")
            value[key] = item
        return value

    @staticmethod
    def body(rule):
        return json.dumps({key: rule[key] for key in ("domain", "conditions", "action")},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def digest(value):
        raw = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _validate(self, value):
        if not isinstance(value, dict) or value.get("schema_version") not in {1, 2}:
            raise ScopeJournalError("scope migration state is invalid")
        expected_keys = self.V1_KEYS if value["schema_version"] == 1 else self.V2_KEYS
        if set(value) != expected_keys \
                or value["phase"] not in {"prepared", "active", "committed"}:
            raise ScopeJournalError("scope migration state is invalid")
        if value["schema_version"] == 2:
            try:
                pinned = canonical_email_list(
                    value["desired_pinned"], allow_empty=True, reject_duplicates=True)
                targets = canonical_email_list(
                    value["desired_targets"], allow_empty=False, reject_duplicates=True)
            except CanonicalEmailError:
                raise ScopeJournalError("scope migration state is invalid") from None
            applied = value["config_applied_sha256"]
            if (pinned != value["desired_pinned"] or targets != value["desired_targets"]
                    or not set(pinned).issubset(targets)
                    or not isinstance(value["config_before_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", value["config_before_sha256"]) is None
                    or not isinstance(value["config_expected_sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", value["config_expected_sha256"]) is None
                    or (applied is not None and (not isinstance(applied, str)
                        or re.fullmatch(r"[0-9a-f]{64}", applied) is None))):
                raise ScopeJournalError("scope migration state is invalid")
        for key in ("old", "unrelated", "new_ids"):
            if not isinstance(value[key], list) or any(
                    not isinstance(item, dict) or set(item) != {"id", "sha256"}
                    or not isinstance(item["id"], str) or not item["id"]
                    or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                    for item in value[key]):
                raise ScopeJournalError("scope migration state is invalid")
        if len({item["id"] for key in ("old", "unrelated", "new_ids") for item in value[key]}) \
                != sum(len(value[key]) for key in ("old", "unrelated", "new_ids")):
            raise ScopeJournalError("scope migration state is invalid")
        if not isinstance(value["new"], list) or any(
                not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
                for item in value["new"]):
            raise ScopeJournalError("scope migration state is invalid")
        if not isinstance(value["retired_ids"], list) or any(
                not isinstance(item, str) or not item for item in value["retired_ids"]):
            raise ScopeJournalError("scope migration state is invalid")
        if (len(value["retired_ids"]) != len(set(value["retired_ids"]))
                or len(value["new"]) != len(set(value["new"]))
                or len({item["sha256"] for item in value["old"]}) != len(value["old"])
                or any(item["sha256"] not in value["new"] for item in value["new_ids"])
                or not set(value["retired_ids"]).issubset(
                    {item["id"] for item in value["old"] + value["new_ids"]})):
            raise ScopeJournalError("scope migration state is invalid")
        pending = value["pending"]
        if pending is not None and (not isinstance(pending, dict)
                or set(pending) != {"kind", "id", "sha256"}
                or pending["kind"] not in {"add", "delete"}
                or not isinstance(pending["id"], str)
                or re.fullmatch(r"[0-9a-f]{64}", pending["sha256"]) is None):
            raise ScopeJournalError("scope migration state is invalid")
        if pending is not None:
            if pending["kind"] == "add" and (pending["sha256"] not in value["new"]
                    or pending["id"] and pending["id"] not in {i["id"] for i in value["new_ids"]}):
                raise ScopeJournalError("scope migration state is invalid")
            if pending["kind"] == "delete":
                hashes = {i["id"]: i["sha256"] for i in value["old"] + value["new_ids"]}
                if pending["id"] not in hashes or pending["sha256"] != hashes[pending["id"]]:
                    raise ScopeJournalError("scope migration state is invalid")
        if value["phase"] == "prepared" and (pending is not None or value["new_ids"]
                or value["retired_ids"] or (value["schema_version"] == 2
                                             and value["config_applied_sha256"] is not None)):
            raise ScopeJournalError("scope migration state is invalid")
        if value["phase"] == "committed" and (pending is not None
                or {item["id"] for item in value["old"]} - set(value["retired_ids"])
                or sorted(item["sha256"] for item in value["new_ids"]) != sorted(value["new"])
                or (value["schema_version"] == 2
                    and value["config_applied_sha256"] is None)):
            raise ScopeJournalError("scope migration state is invalid")
        return value

    def read_exact(self):
        try:
            body = self.ftps.read_scope_journal(self.kind)
        except (AttributeError, TypeError, ValueError, RuntimeError, OSError) as error:
            raise ScopeJournalError("scope migration state is invalid") from error
        if body is None:
            return None, None
        try:
            if not isinstance(body, bytes) or len(body) > self.LIMIT:
                raise ValueError
            return (self._validate(json.loads(
                body, object_pairs_hook=self._unique_object)), body)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ScopeJournalError("scope migration state is invalid") from error

    def read(self):
        return self.read_exact()[0]

    def write(self, value, *, expected=...):
        value = self._validate(value)
        body = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            if self.kind == "target-sync":
                if expected is ...:
                    raise TypeError("target-sync journal write requires exact expected bytes")
                if expected is not None and (not isinstance(expected, bytes)
                                             or not expected):
                    raise TypeError("target-sync journal expected bytes are invalid")
                stored = self.ftps.compare_and_swap_scope_journal(
                    self.kind, expected, body)
            else:
                stored = self.ftps.write_scope_journal(self.kind, body)
        except TypeError:
            raise
        except (AttributeError, TypeError, ValueError, RuntimeError, OSError) as error:
            raise ScopeJournalError("scope migration state readback failed") from error
        if len(body) > self.LIMIT or stored != body:
            raise ScopeJournalError("scope migration state readback failed")
        return body

    def prepare(self, all_rules, old_rules, desired, *, replace_committed=False):
        old_ids = {item["id"] for item in old_rules}
        value = {"schema_version": 1, "phase": "prepared", "pending": None,
                 "old": [{"id": item["id"], "sha256": self.digest(self.body(item))} for item in old_rules],
                 "new": [self.digest(self.body(item)) for item in desired],
                 "unrelated": [{"id": item["id"], "sha256": self.digest(item)}
                               for item in all_rules if item["id"] not in old_ids],
                 "new_ids": [], "retired_ids": []}
        current, current_bytes = self.read_exact()
        if current is None or (replace_committed and current["phase"] == "committed"):
            self.write(value, expected=current_bytes)
            return value
        if any(current[key] != value[key] for key in ("old", "new", "unrelated")):
            raise ScopeJournalError("scope migration baseline mismatch")
        return current

    def prepare_v2(self, all_rules, old_rules, desired, *, desired_pinned,
                   desired_targets, config_before_sha256,
                   config_expected_sha256=None, replace_committed=False):
        old_ids = {item["id"] for item in old_rules}
        value = {
            "schema_version": 2, "phase": "prepared", "pending": None,
            "old": [{"id": item["id"], "sha256": self.digest(self.body(item))}
                    for item in old_rules],
            "new": [self.digest(self.body(item)) for item in desired],
            "unrelated": [{"id": item["id"], "sha256": self.digest(item)}
                          for item in all_rules if item["id"] not in old_ids],
            "new_ids": [], "retired_ids": [],
            "desired_pinned": desired_pinned,
            "desired_targets": desired_targets,
            "config_before_sha256": config_before_sha256,
            "config_expected_sha256": (config_before_sha256 if
                                        config_expected_sha256 is None else
                                        config_expected_sha256),
            "config_applied_sha256": None,
        }
        self._validate(value)
        current, current_bytes = self.read_exact()
        if current is None or (replace_committed and current["phase"] == "committed"):
            self.write(value, expected=current_bytes)
            return value
        if current["schema_version"] != 2:
            raise ScopeJournalError("scope migration baseline mismatch")
        if any(current[key] != value[key] for key in (
                "old", "new", "unrelated", "desired_pinned", "desired_targets",
                "config_before_sha256", "config_expected_sha256")):
            raise ScopeJournalError("scope migration baseline mismatch")
        return current

    def pending_authorization(self):
        """Return a validated copy of the single resumable operation, if any."""
        current = self.read()
        if current is None or current["pending"] is None:
            return None
        return dict(current["pending"])
