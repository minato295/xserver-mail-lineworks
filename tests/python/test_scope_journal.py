import copy
import unittest

from manager.scope_journal import ScopeJournal, ScopeJournalError
from tests.python.test_release_deployer import FakeFtps, rule


class ScopeJournalTest(unittest.TestCase):
    def setUp(self):
        self.ftps = FakeFtps()
        self.path = "/private/xserver-mail-lineworks/deploy-transactions/target-sync-scope.json"
        self.journal = ScopeJournal(self.ftps, self.path)
        old = [rule("old-1", "a@example.invalid", "old")]
        desired = [{key: value for key, value in rule(
            "unused", "a@example.invalid", "new").items() if key != "id"}]
        unrelated = rule("keep-1", "b@example.invalid", "keep")
        self.value = self.journal.prepare(old + [unrelated], old, desired)

    def write_journal(self, value):
        _current, raw = self.journal.read_exact()
        return self.journal.write(value, expected=raw)

    def test_write_and_read_require_exact_600_mode_readback(self):
        self.assertEqual(self.value, self.journal.read())
        self.assertIn(("journal-read", "target-sync"), self.ftps.calls)
        self.assertIn(("journal-cas", "target-sync"), self.ftps.calls)

    def test_target_sync_write_requires_explicit_exact_raw_snapshot(self):
        value, raw = self.journal.read_exact()
        changed = copy.deepcopy(value)
        changed["phase"] = "active"
        with self.assertRaises(TypeError):
            self.journal.write(changed)
        stored = self.journal.write(changed, expected=raw)
        self.assertEqual(stored, self.ftps.files[self.path])
        with self.assertRaises(ScopeJournalError):
            self.journal.write(value, expected=raw)

    def test_rejects_ordinary_and_escaped_equivalent_duplicate_json_keys(self):
        path = "/private/xserver-mail-lineworks/deploy-transactions/target-sync-scope.json"
        valid = self.ftps.files[path] if path in self.ftps.files else self.ftps.files[self.path]
        for body in (
            valid.replace(b'"phase":"prepared"',
                          b'"phase":"prepared","phase":"prepared"'),
            valid.replace(b'"phase":"prepared"',
                          b'"phase":"prepared","\\u0070hase":"prepared"'),
        ):
            self.ftps.files[path] = body
            self.ftps.files[self.path] = body
            with self.subTest(body=body), self.assertRaises(ScopeJournalError):
                self.journal.read()
        self.ftps.files[path] = valid
        self.ftps.files[self.path] = valid

    def test_rejects_cross_field_duplicates_stale_hashes_and_invalid_pending(self):
        cases = []
        duplicate = copy.deepcopy(self.value); duplicate["old"].append(dict(duplicate["old"][0])); cases.append(duplicate)
        stale = copy.deepcopy(self.value); stale["new_ids"] = [{"id": "new-1", "sha256": "f" * 64}]; cases.append(stale)
        retired = copy.deepcopy(self.value); retired["retired_ids"] = ["unknown"]; cases.append(retired)
        pending = copy.deepcopy(self.value); pending["pending"] = {"kind": "delete", "id": "old-1", "sha256": "0" * 64}; cases.append(pending)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ScopeJournalError):
                self.write_journal(value)

    def test_existing_prepare_requires_exact_immutable_baseline(self):
        changed = rule("old-1", "a@example.invalid", "changed")
        desired = [{key: value for key, value in rule(
            "unused", "a@example.invalid", "new").items() if key != "id"}]
        with self.assertRaises(ScopeJournalError):
            self.journal.prepare([changed], [changed], desired)

    def test_interrupted_add_can_resume_only_for_the_authorized_body_hash(self):
        value = copy.deepcopy(self.value)
        value["phase"] = "active"
        value["pending"] = {"kind": "add", "id": "", "sha256": value["new"][0]}
        self.write_journal(value)
        self.assertEqual(value["pending"], self.journal.pending_authorization())
        unknown = copy.deepcopy(value)
        unknown["pending"]["sha256"] = "f" * 64
        with self.assertRaises(ScopeJournalError):
            self.write_journal(unknown)

    def test_interrupted_delete_can_resume_only_for_the_authorized_rule(self):
        value = copy.deepcopy(self.value)
        value["phase"] = "active"
        value["pending"] = {"kind": "delete", **value["old"][0]}
        self.write_journal(value)
        self.assertEqual(value["pending"], self.journal.pending_authorization())
        unknown = copy.deepcopy(value)
        unknown["pending"]["id"] = "unknown"
        with self.assertRaises(ScopeJournalError):
            self.write_journal(unknown)

    def test_prepare_v2_persists_exact_pinned_intent_and_config_digest(self):
        self.ftps = FakeFtps()
        self.journal = ScopeJournal(self.ftps, self.path)
        old = [rule("old-1", "a@example.invalid", "old")]
        desired = [{key: value for key, value in rule(
            "unused", "a@example.invalid", "new").items() if key != "id"}]
        state = self.journal.prepare_v2(
            old, old, desired,
            desired_pinned=["external-info@example.invalid"],
            desired_targets=["a@example.invalid", "external-info@example.invalid"],
            config_before_sha256="a" * 64,
            config_expected_sha256="b" * 64,
            replace_committed=True,
        )
        self.assertEqual(2, state["schema_version"])
        self.assertEqual(["external-info@example.invalid"],
                         self.journal.read()["desired_pinned"])
        self.assertEqual(["a@example.invalid", "external-info@example.invalid"],
                         state["desired_targets"])
        self.assertEqual("a" * 64, state["config_before_sha256"])
        self.assertEqual("b" * 64, state["config_expected_sha256"])
        self.assertIsNone(state["config_applied_sha256"])

    def test_v2_rejects_noncanonical_intent_bad_digest_and_phase_invariants(self):
        self.ftps = FakeFtps()
        self.journal = ScopeJournal(self.ftps, self.path)
        old = [rule("old-1", "a@example.invalid", "old")]
        desired = [{key: value for key, value in rule(
            "unused", "a@example.invalid", "new").items() if key != "id"}]
        state = self.journal.prepare_v2(
            old, old, desired, desired_pinned=["b@example.invalid"],
            desired_targets=["a@example.invalid", "b@example.invalid"],
            config_before_sha256="a" * 64,
            config_expected_sha256="b" * 64, replace_committed=True,
        )
        cases = []
        bad = copy.deepcopy(state); bad["desired_pinned"] = ["b@EXAMPLE.INVALID"]; cases.append(bad)
        bad = copy.deepcopy(state); bad["desired_targets"].reverse(); cases.append(bad)
        bad = copy.deepcopy(state); bad["config_before_sha256"] = "secret"; cases.append(bad)
        bad = copy.deepcopy(state); bad["desired_pinned"] = ["c@example.invalid"]; cases.append(bad)
        bad = copy.deepcopy(state); bad["phase"] = "prepared"; bad["retired_ids"] = ["old-1"]; cases.append(bad)
        bad = copy.deepcopy(state); bad["phase"] = "committed"
        bad["retired_ids"] = ["old-1"]
        bad["new_ids"] = [{"id": "new-1", "sha256": bad["new"][0]}]
        cases.append(bad)
        bad = copy.deepcopy(state); bad["config_applied_sha256"] = "invalid"; cases.append(bad)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ScopeJournalError):
                self.write_journal(value)

    def test_noncommitted_v1_cannot_be_replaced_but_committed_v1_can(self):
        old = [rule("old-1", "a@example.invalid", "old")]
        desired = [{key: value for key, value in rule(
            "unused", "a@example.invalid", "new").items() if key != "id"}]
        with self.assertRaises(ScopeJournalError):
            self.journal.prepare_v2(
                old, old, desired, desired_pinned=[],
                desired_targets=["a@example.invalid"],
                config_before_sha256="a" * 64,
                config_expected_sha256="b" * 64, replace_committed=True,
            )
        committed = copy.deepcopy(self.value)
        committed["phase"] = "committed"
        committed["new_ids"] = [{"id": "new-1", "sha256": committed["new"][0]}]
        committed["retired_ids"] = ["old-1"]
        self.write_journal(committed)
        state = self.journal.prepare_v2(
            old, old, desired, desired_pinned=[],
            desired_targets=["a@example.invalid"],
            config_before_sha256="a" * 64,
            config_expected_sha256="b" * 64, replace_committed=True,
        )
        self.assertEqual(2, state["schema_version"])


if __name__ == "__main__":
    unittest.main()
