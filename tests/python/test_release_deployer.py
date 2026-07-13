import hashlib
import json
import tempfile
import unittest
from ftplib import error_perm
from datetime import datetime, timezone
from pathlib import Path

from manager.release_deployer import (
    ReleaseDeployer, ReleaseDeploymentError, build_manifest, build_stable_manifest,
)


class FakeFtps:
    def __init__(self):
        self.files = {}
        self.calls = []

    def deploy_release(self, local_dir, remote_root):
        self.calls.append(("deploy", remote_root))
        root = Path(local_dir)
        for path in root.rglob("*"):
            if path.is_file():
                self.files[remote_root.rstrip("/") + "/" + path.relative_to(root).as_posix()] = path.read_bytes()

    def read_bytes(self, remote_path, *, limit):
        self.calls.append(("read", remote_path, limit))
        return self.files[remote_path]

    def replace_bytes_atomic(self, remote_path, body, *, mode):
        self.calls.append(("replace", remote_path, mode))
        self.files[remote_path] = body
        return self.files[remote_path]

    def assert_file_mode(self, remote_path, expected_mode):
        self.calls.append(("mode", remote_path, expected_mode))

    @staticmethod
    def _journal_path(kind):
        return ({
            "target-sync": "/private/xserver-mail-lineworks/deploy-transactions/target-sync-scope.json",
            "filter": "/home/example/private/xserver-mail-lineworks/deploy-transactions/filter-scope.json",
        })[kind]

    def read_scope_journal(self, kind):
        path = self._journal_path(kind)
        self.calls.append(("journal-read", kind))
        return self.files.get(path)

    def write_scope_journal(self, kind, body):
        path = self._journal_path(kind)
        self.calls.append(("journal-write", kind))
        self.files[path] = body
        return body

    def compare_and_swap_scope_journal(self, kind, expected, desired):
        path = self._journal_path(kind)
        self.calls.append(("journal-cas", kind))
        current = self.files.get(path)
        if current == desired:
            return desired
        if current != expected:
            raise RuntimeError("journal CAS conflict")
        self.files[path] = desired
        return desired


class FakeValidator:
    def __init__(self, result=None):
        self.result = result or {
            "manifest": "PASS", "php_cli": "PASS", "autoload": "PASS",
            "absolute_cli_dry_run": "PASS", "public_root": "PASS", "symlinks": 0,
        }
        self.calls = []

    def validate_release(self, manifest, **kwargs):
        self.calls.append((manifest, kwargs))
        return dict(self.result)


class NoMutationApi:
    def __init__(self):
        self.mutations = []

    def add_filter(self, rule):
        self.mutations.append(("add", rule))

    def delete_filter(self, rule_id):
        self.mutations.append(("delete", rule_id))


def rule(rule_id, recipient, target):
    domain = recipient.split("@", 1)[1]
    return {
        "id": rule_id, "domain": domain,
        "conditions": [{"keyword": recipient, "field": "to", "match_type": "match"}],
        "action": {"type": "mail_address", "target": target, "method": "copy"},
    }


class MigrationApi:
    def __init__(self, rules):
        self.rules = [json.loads(json.dumps(item)) for item in rules]
        self.calls = []
        self.next_id = 1

    def snapshot_filters(self):
        self.calls.append(("read",))
        return json.loads(json.dumps(sorted(self.rules, key=lambda item: item["id"])))

    def add_filter(self, item):
        self.calls.append(("add", item["domain"]))
        created = dict(json.loads(json.dumps(item)), id="created-%d" % self.next_id)
        self.next_id += 1
        self.rules.append(created)
        return {"id": created["id"]}

    def delete_filter(self, rule_id):
        self.calls.append(("delete", rule_id))
        self.rules = [item for item in self.rules if item["id"] != rule_id]
        return {}


class ReleaseDeployerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "run.php").write_bytes(b"<?php exit(0);\n")
        (self.root / "data.txt").write_bytes(b"payload\n")
        (self.root / "bin" / "run.php").chmod(0o700)
        (self.root / "data.txt").chmod(0o600)
        self.ftps = FakeFtps()
        self.validator = FakeValidator()
        self.api = NoMutationApi()
        self.deployer = ReleaseDeployer(self.ftps, self.validator, self.api)

    def migration_kwargs(self):
        return {
            "state_path": "/home/example/private/state/filter-migration.json",
            "max_downtime_seconds": 120,
            "confirm_maintenance": lambda _: "通知停止とメールボックス確認を了承します",
        }

    def test_manifest_is_canonical_sorted_and_contains_exact_modes(self):
        manifest = build_manifest(self.root)
        files = [item for item in manifest if item["type"] == "file"]
        self.assertEqual(["bin/run.php", "data.txt"], [item["path"] for item in files])
        self.assertEqual([0o700, 0o600], [item["mode"] for item in files])
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(encoded, self.deployer.canonical_manifest(manifest))

    def test_stable_manifest_binds_entrypoint_and_runtime_php_dependencies(self):
        manifest = build_manifest(self.root)
        stable = build_stable_manifest(
            manifest, "bin/run.php", preload_paths=(), source_root=self.root,
        )
        self.assertEqual(1, stable["schema_version"])
        self.assertEqual("bin/run.php", stable["entrypoint"]["path"])
        self.assertEqual(0o700, stable["entrypoint"]["mode"])
        self.assertEqual([], stable["runtime"])
        with self.assertRaises(ReleaseDeploymentError):
            build_stable_manifest(manifest, "missing.php", preload_paths=(), source_root=self.root)

    def test_stable_manifest_excludes_php_named_templates_and_rejects_bom_or_leading_whitespace(self):
        fixtures = {
            "vendor/template.php": b"<html><?= $value ?></html>\n",
            "vendor/bom.php": b"\xef\xbb\xbf<?php function bom_fixture(): void {}\n",
            "vendor/leading.php": b" \n<?php function leading_fixture(): void {}\n",
            "vendor/source.php": b"<?php\nfunction source_fixture(): void {}\n",
        }
        (self.root / "vendor").mkdir()
        for relative, body in fixtures.items():
            path = self.root / relative
            path.write_bytes(body)
            path.chmod(0o600)
        manifest = build_manifest(self.root)
        stable = build_stable_manifest(
            manifest, "bin/run.php", preload_paths=(), source_root=self.root,
        )
        self.assertEqual(["vendor/source.php"], [item["path"] for item in stable["runtime"]])
        self.assertEqual(
            sorted(fixtures),
            sorted(item["path"] for item in manifest if item["path"].startswith("vendor/")),
        )

    def test_stable_manifest_requires_entrypoint_and_preloads_to_open_as_php(self):
        (self.root / "template.php").write_bytes(b"template text\n")
        (self.root / "template.php").chmod(0o600)
        manifest = build_manifest(self.root)
        with self.assertRaisesRegex(ReleaseDeploymentError, "stable manifest invalid"):
            build_stable_manifest(
                manifest, "bin/run.php", preload_paths=("template.php",), source_root=self.root,
            )
        (self.root / "bin/run.php").write_bytes(b"\xef\xbb\xbf<?php exit(0);\n")
        (self.root / "bin/run.php").chmod(0o700)
        with self.assertRaisesRegex(ReleaseDeploymentError, "stable manifest invalid"):
            build_stable_manifest(
                build_manifest(self.root), "bin/run.php", preload_paths=(), source_root=self.root,
            )

    def test_stage_reads_every_uploaded_file_before_ssh_validation_and_never_mutates_api(self):
        result = self.deployer.stage_and_validate(self.root, "/home/example/private/releases/release-test")
        self.assertEqual("PASS", result["manifest"])
        reads = [call[1] for call in self.ftps.calls if call[0] == "read"]
        self.assertEqual([
            "/home/example/private/releases/release-test/bin/run.php",
            "/home/example/private/releases/release-test/data.txt",
        ], reads)
        self.assertEqual(1, len(self.validator.calls))
        self.assertEqual([], self.api.mutations)

    def test_stage_stops_before_validator_on_download_hash_mismatch(self):
        original = self.ftps.deploy_release
        def corrupt(local_dir, remote_root):
            original(local_dir, remote_root)
            self.ftps.files[remote_root + "/data.txt"] = b"changed"
        self.ftps.deploy_release = corrupt
        with self.assertRaises(ReleaseDeploymentError):
            self.deployer.stage_and_validate(self.root, "/home/example/private/releases/release-test")
        self.assertEqual([], self.validator.calls)
        self.assertEqual([], self.api.mutations)

    def test_validation_failure_never_changes_locator_or_filters(self):
        self.validator.result["autoload"] = "FAIL"
        with self.assertRaises(ReleaseDeploymentError):
            self.deployer.stage_and_validate(self.root, "/home/example/private/releases/release-test")
        self.assertFalse(any(call[0] == "replace" for call in self.ftps.calls))
        self.assertEqual([], self.api.mutations)

    def test_switch_locator_uses_atomic_replace_readback_and_no_api(self):
        locator = {
            "schema_version": 1, "release_id": "release-test",
            "release_path": "/home/example/private/releases/release-test",
            "entrypoint": "bin/run.php", "manifest_sha256": "a" * 64,
            "config_path": "/home/example/private/config.json",
        }
        path = "/home/example/private/state/active-release.json"
        digest = self.deployer.switch_locator(path, locator)
        expected = (json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self.assertEqual(hashlib.sha256(expected).hexdigest(), digest)
        self.assertEqual(expected, self.ftps.files[path])
        self.assertEqual([], self.api.mutations)

    def test_bootstrap_overlap_reads_full_set_after_every_mutation_and_handles_duplicates(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/stable.php"
        old = [rule("old-1", "a@example.invalid", old_target),
               rule("old-2", "a@example.invalid", old_target),
               rule("old-3", "b@example.invalid", old_target)]
        old[2]["domain"] = "two.example.invalid"
        desired = [{"domain": r["domain"], "conditions": r["conditions"],
                    "action": dict(r["action"], target=new_target)} for r in old]
        api = MigrationApi(old)
        deployer = ReleaseDeployer(self.ftps, self.validator, api)
        transitions = []
        result = deployer.bootstrap_migrate(old, desired, **self.migration_kwargs(),
                                            switch_pair=lambda: transitions.append("new"))
        self.assertEqual("maintenance-committed", result["status"])
        self.assertEqual(3, len([c for c in api.calls if c[0] == "add"]))
        self.assertEqual(3, len([c for c in api.calls if c[0] == "delete"]))
        self.assertEqual(1 + 6, len([c for c in api.calls if c[0] == "read"]))
        self.assertEqual(sorted(new_target for _ in range(3)),
                         sorted(r["action"]["target"] for r in api.rules))
        self.assertEqual(["new"], transitions)

    def test_bootstrap_zero_rules_is_read_only_and_reorder_is_ignored(self):
        api = MigrationApi([])
        deployer = ReleaseDeployer(self.ftps, self.validator, api)
        self.assertEqual("committed", deployer.bootstrap_migrate([], [], **self.migration_kwargs())["status"])
        self.assertEqual([("read",)], api.calls)

    def test_unknown_rule_appearing_after_add_stops_without_deleting_old(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/stable.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        api = MigrationApi(old)
        original_add = api.add_filter
        def add_with_intruder(item):
            result = original_add(item)
            api.rules.append(rule("intruder", "x@example.invalid", old_target))
            return result
        api.add_filter = add_with_intruder
        with self.assertRaisesRegex(ReleaseDeploymentError, "unexpected filter set"):
            ReleaseDeployer(self.ftps, self.validator, api).bootstrap_migrate(
                old, desired, **self.migration_kwargs(), switch_pair=lambda: None)

    def test_maintenance_requires_exact_japanese_confirmation_and_marker_readback(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/stable.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        api = MigrationApi(old)
        deployer = ReleaseDeployer(self.ftps, self.validator, api)
        with self.assertRaisesRegex(ReleaseDeploymentError, "明示確認"):
            deployer.bootstrap_migrate(old, desired, state_path=self.migration_kwargs()["state_path"],
                                       max_downtime_seconds=120, confirm_maintenance=lambda _: "はい")
        self.assertFalse(any(c[0] in ("add", "delete") for c in api.calls))
        result = deployer.bootstrap_migrate(
            old, desired, state_path=self.migration_kwargs()["state_path"], max_downtime_seconds=120,
            confirm_maintenance=lambda message: "通知停止とメールボックス確認を了承します",
            maintenance_marker_path="/home/example/private/state/maintenance.json",
            switch_pair=lambda: api.calls.append(("switch-pair",)))
        self.assertEqual("maintenance-committed", result["status"])
        self.assertIn("window_started_at", result)
        self.assertIn("window_ended_at", result)
        marker = self.ftps.files["/home/example/private/state/maintenance.json"]
        self.assertNotIn(b"@", marker)
        operations = [call[0] for call in api.calls]
        self.assertLess(operations.index("delete"), operations.index("switch-pair"))
        self.assertLess(operations.index("switch-pair"), operations.index("add"))

    def test_fault_hook_runs_before_and_after_every_mutation(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/stable.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        points = []
        ReleaseDeployer(self.ftps, self.validator, MigrationApi(old)).bootstrap_migrate(
            old, desired, **self.migration_kwargs(), fault=points.append,
            switch_pair=lambda: None)
        for required in ("before_state_write", "after_state_write", "before_delete", "after_delete",
                         "before_switch", "after_switch", "before_add", "after_add"):
            self.assertIn(required, points)

    def test_convergence_table_rejects_unknown_and_classifies_supported_pairs(self):
        classify = ReleaseDeployer.classify_convergence
        self.assertEqual("overlap-forward", classify("old", "old", "new"))
        self.assertEqual("overlap-rollback", classify("old", "old+new", "new"))
        self.assertEqual("cleanup", classify("new", "old+new", "new"))
        self.assertEqual("committed", classify("new", "new", "new"))
        self.assertEqual("isolate", classify("unknown", "old", "new"))

    def test_overlap_never_deletes_old_before_new_pair_is_confirmed(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/stable.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        api = MigrationApi(old)
        with self.assertRaisesRegex(ReleaseDeploymentError, "switch"):
            ReleaseDeployer(self.ftps, self.validator, api).bootstrap_migrate(
                old, desired, **self.migration_kwargs())
        self.assertFalse(any(c[0] == "delete" for c in api.calls))

    def test_recovery_converges_overlap_by_locator_identity_without_adding(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/stable.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        overlap = old + [dict(desired[0], id="new-1")]
        api = MigrationApi(overlap)
        with self.assertRaises(ReleaseDeploymentError):
            ReleaseDeployer(self.ftps, self.validator, api).bootstrap_migrate(
                old, desired, **self.migration_kwargs(), locator_pair="new")
        self.assertFalse(any(c[0] in {"add", "delete"} for c in api.calls))

    def test_overlap_rejects_boolean_without_structured_probe_evidence(self):
        old = [rule("old-1", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/old.php")]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/new.php").items() if k != "id"}]
        with self.assertRaisesRegex(ReleaseDeploymentError, "disabled"):
            ReleaseDeployer(self.ftps, self.validator, MigrationApi(old)).bootstrap_migrate(
                old, desired, state_path=self.migration_kwargs()["state_path"], overlap_evidence=True)

    def test_overlap_evidence_is_disabled_and_exact_120_second_maintenance_is_required(self):
        old = [rule("old-1", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/old.php")]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/new.php").items() if k != "id"}]
        with self.assertRaisesRegex(ReleaseDeploymentError, "disabled"):
            ReleaseDeployer(self.ftps, self.validator, MigrationApi(old)).bootstrap_migrate(
                old, desired, state_path=self.migration_kwargs()["state_path"], overlap_evidence={"forged": True})
        for seconds in (119, 121):
            with self.subTest(seconds=seconds), self.assertRaises(ReleaseDeploymentError):
                ReleaseDeployer(self.ftps, self.validator, MigrationApi(old)).bootstrap_migrate(
                    old, desired, state_path=self.migration_kwargs()["state_path"],
                    max_downtime_seconds=seconds,
                    confirm_maintenance=lambda _: "通知停止とメールボックス確認を了承します",
                    switch_pair=lambda: None)

    def test_malformed_stable_manifest_has_fixed_error(self):
        for malformed, preloads in (([{"path": [], "type": "file"}], ()), ([], None), (None, ())):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ReleaseDeploymentError, "stable manifest invalid"):
                build_stable_manifest(malformed, "bin/run.php", preload_paths=preloads, source_root=self.root)

    def test_server_added_fields_are_normalized_during_full_readback(self):
        old = [dict(rule("old-1", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/old.php"), priority=1)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/new.php").items() if k != "id"}]
        api = MigrationApi(old)
        original = api.add_filter
        api.add_filter = lambda item: original(dict(item, priority=99))
        result = ReleaseDeployer(self.ftps, self.validator, api).bootstrap_migrate(
            old, desired, **self.migration_kwargs(), switch_pair=lambda: None)
        self.assertEqual("maintenance-committed", result["status"])

    def test_stable_manifest_rejects_invalid_preloads_and_duplicate_paths(self):
        manifest = build_manifest(self.root)
        manifest.append(dict(manifest[-1]))
        with self.assertRaises(ReleaseDeploymentError):
            build_stable_manifest(manifest, "bin/run.php", preload_paths=(), source_root=self.root)
        clean = build_manifest(self.root)
        for invalid in (("bin/run.php",), ("data.txt",), ("missing.php",)):
            with self.assertRaises(ReleaseDeploymentError):
                build_stable_manifest(clean, "bin/run.php", preload_paths=invalid, source_root=self.root)

    def test_stable_manifest_roundtrips_exact_bootstrap_schema(self):
        dependency = self.root / "bin/dependency.php"
        dependency.write_text("<?php function fixture_dependency(): void {}\n", encoding="utf-8")
        dependency.chmod(0o600)
        stable = build_stable_manifest(build_manifest(self.root), "bin/run.php",
                                       preload_paths=("bin/dependency.php",), source_root=self.root)
        decoded = json.loads(json.dumps(stable, sort_keys=True, separators=(",", ":")))
        self.assertEqual({"schema_version", "entrypoint", "runtime"}, set(decoded))
        self.assertEqual(["bin/dependency.php"], [item["path"] for item in decoded["runtime"]])
        self.assertIs(True, decoded["runtime"][0]["preload"])

    def test_missing_remote_state_550_allows_first_durable_snapshot(self):
        old = [rule("old-1", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/old.php")]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/new.php").items() if k != "id"}]
        original = self.ftps.read_bytes
        self.ftps.read_bytes = lambda path, *, limit: (_ for _ in ()).throw(error_perm("550 missing")) \
            if path.endswith("filter-migration.json") else original(path, limit=limit)
        result = ReleaseDeployer(self.ftps, self.validator, MigrationApi(old)).bootstrap_migrate(
            old, desired, **self.migration_kwargs(), switch_pair=lambda: None)
        self.assertEqual("maintenance-committed", result["status"])

    def test_every_mutation_fault_restarts_into_a_valid_table_state(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/new.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        for point in ("before_state_write", "after_state_write", "before_add", "after_add",
                      "before_switch", "after_switch", "before_delete", "after_delete"):
            with self.subTest(point=point):
                ftps = FakeFtps()
                api = MigrationApi(old)
                locator = ["old"]
                def switch(): locator[0] = "new"
                def crash(actual):
                    if actual == point: raise RuntimeError("process killed")
                deployer = ReleaseDeployer(ftps, self.validator, api)
                with self.assertRaises(RuntimeError):
                    deployer.bootstrap_migrate(old, desired, **self.migration_kwargs(),
                                               locator_pair=locator[0], switch_pair=switch, fault=crash)
                result = deployer.bootstrap_migrate(old, desired, **self.migration_kwargs(),
                                                    locator_pair=locator[0], switch_pair=switch)
                self.assertEqual("maintenance-committed", result["status"])
                targets = {item["action"]["target"] for item in api.rules}
                self.assertIn(targets, ({old_target}, {new_target}))

    def test_old_pair_overlap_rolls_back_and_unknown_state_never_mutates_filters(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/new.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        api = MigrationApi(old + [dict(desired[0], id="new-1")])
        with self.assertRaises(ReleaseDeploymentError):
            ReleaseDeployer(self.ftps, self.validator, api).bootstrap_migrate(
                old, desired, **self.migration_kwargs(), locator_pair="old")
        self.assertFalse(any(call[0] in {"add", "delete"} for call in api.calls))
        unknown = MigrationApi(old + [rule("foreign", "x@example.invalid", old_target)])
        with self.assertRaises(ReleaseDeploymentError):
            ReleaseDeployer(self.ftps, self.validator, unknown).bootstrap_migrate(
                old, desired, **self.migration_kwargs(), locator_pair="old")
        self.assertFalse(any(call[0] in {"add", "delete"} for call in unknown.calls))

    def test_maintenance_window_preserves_mailbox_and_has_no_duplicate_notifications(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/new.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", new_target).items() if k != "id"}]
        api = MigrationApi(old)
        mailbox, notifications = [], []
        def inject(message):
            mailbox.append(message)
            notifications.extend([message for item in api.rules
                                  if item["conditions"][0]["keyword"] == "a@example.invalid"])
        inject("before")
        def fault(point):
            if point == "after_delete":
                inject("window-1"); inject("window-2")
        ReleaseDeployer(self.ftps, self.validator, api).bootstrap_migrate(
            old, desired, state_path=self.migration_kwargs()["state_path"],
            max_downtime_seconds=120,
            confirm_maintenance=lambda _: "通知停止とメールボックス確認を了承します",
            switch_pair=lambda: None, fault=fault)
        inject("after")
        self.assertEqual(["before", "window-1", "window-2", "after"], mailbox)
        self.assertEqual(["before", "after"], notifications)

    def test_maintenance_restarts_from_empty_and_partial_new_sets(self):
        old_target = "|/usr/bin/php8.5 /home/example/private/old.php"
        new_target = "|/usr/bin/php8.5 /home/example/private/new.php"
        old = [rule("old-1", "a@example.invalid", old_target)]
        desired = [{k: v for k, v in rule("x", address, new_target).items() if k != "id"}
                   for address in ("a@example.invalid", "b@example.invalid")]
        for crash_point in ("after_delete", "after_add"):
            with self.subTest(crash_point=crash_point):
                ftps = FakeFtps(); api = MigrationApi(old); locator = ["old"]; add_count = [0]
                def switch(): locator[0] = "new"
                def crash(point):
                    if point == crash_point:
                        if point != "after_add" or add_count[0] == 0:
                            add_count[0] += 1
                            raise RuntimeError("process killed")
                deployer = ReleaseDeployer(ftps, self.validator, api)
                with self.assertRaises(RuntimeError):
                    deployer.bootstrap_migrate(
                        old, desired, state_path=self.migration_kwargs()["state_path"],
                        max_downtime_seconds=120,
                        confirm_maintenance=lambda _: "通知停止とメールボックス確認を了承します",
                        switch_pair=switch, fault=crash)
                result = deployer.bootstrap_migrate(
                    old, desired, state_path=self.migration_kwargs()["state_path"],
                    locator_pair=locator[0], max_downtime_seconds=120, switch_pair=switch)
                self.assertEqual("maintenance-committed", result["status"])
                self.assertEqual(2, len(api.rules))

    def test_authorized_deadline_is_durable_before_delete_and_never_resets(self):
        old = [rule("old-1", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/old.php")]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/new.php").items() if k != "id"}]
        api = MigrationApi(old); now = [datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)]
        deployer = ReleaseDeployer(self.ftps, self.validator, api)
        with self.assertRaises(RuntimeError):
            deployer.bootstrap_migrate(
                old, desired, **self.migration_kwargs(), switch_pair=lambda: None,
                clock=lambda: now[0],
                fault=lambda point: (_ for _ in ()).throw(RuntimeError("killed"))
                if point == "after_delete" else None)
        marker = json.loads(self.ftps.files[self.migration_kwargs()["state_path"]])
        self.assertEqual("2026-07-11T12:02:00+00:00", marker["deadline_at"])
        now[0] = datetime(2026, 7, 11, 12, 2, 1, tzinfo=timezone.utc)
        mutations = len([call for call in api.calls if call[0] in {"add", "delete"}])
        with self.assertRaisesRegex(ReleaseDeploymentError, "deadline"):
            deployer.bootstrap_migrate(old, desired, **self.migration_kwargs(),
                                       locator_pair="old", switch_pair=lambda: None,
                                       clock=lambda: now[0])
        self.assertEqual(mutations, len([call for call in api.calls if call[0] in {"add", "delete"}]))

    def test_clock_rollback_before_authorization_epoch_has_zero_mutation(self):
        old = [rule("old-1", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/old.php")]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/new.php").items() if k != "id"}]
        api = MigrationApi(old); locator = ["old"]
        deployer = ReleaseDeployer(self.ftps, self.validator, api)
        with self.assertRaises(RuntimeError):
            deployer.bootstrap_migrate(
                old, desired, **self.migration_kwargs(),
                switch_pair=lambda: locator.__setitem__(0, "new"),
                clock=lambda: datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
                fault=lambda point: (_ for _ in ()).throw(RuntimeError("killed"))
                if point == "before_delete" else None)
        before = len([call for call in api.calls if call[0] in {"add", "delete"}])
        with self.assertRaisesRegex(ReleaseDeploymentError, "deadline"):
            deployer.bootstrap_migrate(
                old, desired, **self.migration_kwargs(), locator_pair=locator[0],
                switch_pair=lambda: locator.__setitem__(0, "new"),
                clock=lambda: datetime(2026, 7, 11, 11, 59, tzinfo=timezone.utc))
        self.assertEqual(before, len([call for call in api.calls if call[0] in {"add", "delete"}]))
        self.assertEqual("old", locator[0])

    def test_tampered_deadline_and_phase_timestamp_order_never_mutate(self):
        old = [rule("old-1", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/old.php")]
        desired = [{k: v for k, v in rule("x", "a@example.invalid", "|/usr/bin/php8.5 /home/example/private/new.php").items() if k != "id"}]
        path = self.migration_kwargs()["state_path"]
        for mutation in ("deadline", "prepared-with-auth", "window-before-auth", "end-before-start"):
            with self.subTest(mutation=mutation):
                ftps = FakeFtps(); api = MigrationApi(old)
                deployer = ReleaseDeployer(ftps, self.validator, api)
                with self.assertRaises(RuntimeError):
                    deployer.bootstrap_migrate(
                        old, desired, **self.migration_kwargs(), switch_pair=lambda: None,
                        clock=lambda: datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
                        fault=lambda point: (_ for _ in ()).throw(RuntimeError("killed"))
                        if point == "after_delete" else None)
                state = json.loads(ftps.files[path])
                if mutation == "deadline":
                    state["deadline_at"] = "2026-07-11T12:02:01+00:00"
                elif mutation == "prepared-with-auth":
                    state.update(phase="prepared")
                elif mutation == "window-before-auth":
                    state.update(phase="maintenance-window",
                                 window_started_at="2026-07-11T11:59:59+00:00")
                else:
                    state.update(phase="committed", locator_pair="new",
                                 window_started_at="2026-07-11T12:00:10+00:00",
                                 window_ended_at="2026-07-11T12:00:09+00:00")
                ftps.files[path] = (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
                mutations = len([call for call in api.calls if call[0] in {"add", "delete"}])
                with self.assertRaisesRegex(ReleaseDeploymentError, "migration state"):
                    deployer.bootstrap_migrate(old, desired, **self.migration_kwargs(),
                                               locator_pair="old", switch_pair=lambda: None)
                self.assertEqual(mutations, len([call for call in api.calls if call[0] in {"add", "delete"}]))


if __name__ == "__main__":
    unittest.main()
