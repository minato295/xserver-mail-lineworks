import json
import hashlib
import os
import shutil
import tempfile
import unittest
from ftplib import error_perm
from pathlib import Path
from types import SimpleNamespace

from manager.remote_validator import RemoteValidationError
from manager.release_workflow import ReleaseWorkflow, ReleaseWorkflowError


class FakeFtps:
    def __init__(self):
        self.files = {}
        self.deployments = []
        self.modes = {}
        self.cleanups = []
        self.verifications = []
        self.events = []
        self.after_replace = None
        self.raise_after_replace = False
        self.before_deploy = None
        self.after_deploy = None
        self.before_publish = None
        self.after_publish = None
        self.hash_verifications = []
        self.subset_error = None

    def read_optional_bytes(self, path, *, limit):
        return self.files.get(path)

    def read_bytes(self, path, *, limit):
        value = self.files[path]
        if len(value) > limit:
            raise RuntimeError("oversize")
        return value

    def deploy_release(self, local, remote):
        if self.before_deploy is not None:
            self.before_deploy(remote)
        self.deployments.append(remote)
        self.events.append(("deploy", remote))
        root = Path(local)
        for path in root.rglob("*"):
            if path.is_file():
                self.files[remote.rstrip("/") + "/" + path.relative_to(root).as_posix()] = path.read_bytes()
                self.modes[remote.rstrip("/") + "/" + path.relative_to(root).as_posix()] = format(
                    path.stat().st_mode & 0o777, "03o"
                )
        if self.after_deploy is not None:
            self.after_deploy(remote)

    def assert_file_mode(self, path, mode):
        if self.modes.get(path) != mode:
            raise RuntimeError("mode mismatch")

    def verify_private_files(self, expected, *, allow_all_missing=False):
        self.verifications.append((dict(expected), allow_all_missing))
        present = [path in self.files for path in expected]
        if not any(present) and allow_all_missing:
            return False
        if not all(present):
            raise RuntimeError("partial")
        for path, (body, mode) in expected.items():
            if self.files[path] != body or self.modes.get(path) != mode:
                raise RuntimeError("mismatch")
        return True

    def replace_bytes_atomic(self, remote_path, body, *, mode="600"):
        self.files[remote_path] = body
        self.modes[remote_path] = mode
        self.events.append(("replace", remote_path))
        if self.after_replace is not None:
            self.after_replace(remote_path)
        if self.raise_after_replace:
            self.raise_after_replace = False
            raise RuntimeError("rename result unavailable")
        return body

    def verify_private_file_hashes(self, expected):
        self.hash_verifications.append(frozenset(expected))
        for path, item in expected.items():
            if (path not in self.files or len(self.files[path]) != item["size"]
                    or __import__("hashlib").sha256(self.files[path]).hexdigest() != item["sha256"]
                    or self.modes.get(path) != item["mode"]):
                raise RuntimeError("hash mismatch")
        return True

    def verify_private_file_hash_subset(self, expected, present_paths):
        self.hash_verifications.append(frozenset(present_paths))
        if self.subset_error is not None:
            raise self.subset_error
        for path in present_paths:
            item = expected[path]
            if path not in self.files:
                raise RuntimeError("required subset missing")
            if (len(self.files[path]) != item["size"]
                    or hashlib.sha256(self.files[path]).hexdigest() != item["sha256"]
                    or self.modes.get(path) != item["mode"]):
                raise RuntimeError("hash subset mismatch")
        return frozenset(present_paths)

    def publish_directory(self, staging, final):
        if self.before_publish is not None:
            self.before_publish(staging, final)
        self.events.append(("publish", staging, final))
        moved = {}
        for path, body in list(self.files.items()):
            if path.startswith(staging + "/"):
                target = final + path[len(staging):]
                moved[target] = body
                self.modes[target] = self.modes.pop(path)
                del self.files[path]
        self.files.update(moved)
        if self.after_publish is not None:
            self.after_publish(staging, final)

    def delete_exact_tree(self, root, relative_files, relative_directories):
        self.cleanups.append(root)
        for relative in relative_files:
            self.files.pop(root + "/" + relative, None)
            self.modes.pop(root + "/" + relative, None)


class FakeRemoteValidator:
    def __init__(self, ftps):
        self.ftps = ftps
        self.inspections = []
        self.invalid_directories = set()
        self.after_inspect = None
        self.strict_mismatches = []
        self.details_override = None
    def inspect_fixed_runtime_details(self, root, entries, *, expected_hosts):
        self.inspections.append((root, entries, expected_hosts))
        if any(candidate_root == root and relative in entries
               for candidate_root, relative in self.invalid_directories):
            raise RemoteValidationError("strict directory mismatch")
        suffix = root.split("/home/example", 1)[-1]
        ignored = ("deploy-transactions/", "logs/", "releases/", "state/")
        present_files = {path[len(suffix) + 1:]: path for path in self.ftps.files
                         if path.startswith(suffix + "/")
                         and not path[len(suffix) + 1:].startswith(ignored)}
        actual = {}
        for relative, remote in present_files.items():
            body = self.ftps.files[remote]
            actual[relative] = {
                "type": "file", "mode": int(self.ftps.modes[remote], 8),
                "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            }
            parent = relative.rpartition("/")[0]
            while parent:
                actual.setdefault(parent, {
                    "type": "directory", "mode": 0o700, "size": 0, "sha256": None,
                })
                parent = parent.rpartition("/")[0]
        if not actual:
            details = {"state": "ABSENT", "present_files": ()}
            if self.details_override is not None:
                details = self.details_override(root, details)
            return details
        for relative, item in actual.items():
            if relative not in entries or entries[relative] != item:
                self.strict_mismatches.append((root, relative))
                raise RemoteValidationError("strict unexpected or metadata mismatch")
        state = "EXACT" if set(actual) == set(entries) else "PARTIAL"
        if self.after_inspect is not None:
            self.after_inspect(root, entries, state)
        details = {"state": state, "present_files": tuple(sorted(present_files))}
        if self.details_override is not None:
            details = self.details_override(root, details)
        return details
    def inspect_fixed_runtime(self, root, entries, *, expected_hosts):
        return self.inspect_fixed_runtime_details(
            root, entries, expected_hosts=expected_hosts)["state"]


class FakeReleaseDeployer:
    def __init__(self):
        self.ftps = FakeFtps()
        self.remote_validator = FakeRemoteValidator(self.ftps)
        self.validation_context = {"expected_hosts": ["example.xsrv.jp"]}
        self.stages = []
        self.switches = []

    def stage_and_validate(self, local, remote, *, validation_root):
        manifest = json.loads((Path(local) / "release-manifest.json").read_text())
        self.stages.append((remote, validation_root, manifest))
        return {"manifest": "PASS"}

    def switch_locator(self, path, locator):
        self.switches.append((path, locator))
        return "digest"


class ReleaseWorkflowTest(unittest.TestCase):
    MIGRATION_ORDER = (
        "src/ReleaseValidator.php",
        "bootstrap/validate-release.php",
        "bootstrap/mail-forward-command.php",
    )

    def _copy_tracked_legacy_assets(self, name="assets"):
        repository = Path(__file__).resolve().parents[2]
        assets = Path(self.temp.name) / name
        assets.mkdir()
        helper = assets / "manage-private-config.php"
        manifest = assets / "legacy-manifest.json"
        shutil.copyfile(repository / "bin/manage-private-config.php", helper)
        shutil.copyfile(repository / "fixed-runtime/legacy-manifest.json", manifest)
        helper.chmod(0o644)
        manifest.chmod(0o644)
        return helper, manifest

    def test_pinned_legacy_asset_constants_match_tracked_assets(self):
        repository = Path(__file__).resolve().parents[2]
        helper = (repository / "bin/manage-private-config.php").read_bytes()
        manifest = (repository / "fixed-runtime/legacy-manifest.json").read_bytes()
        self.assertEqual(ReleaseWorkflow.LEGACY_HELPER_SIZE, len(helper))
        self.assertEqual(ReleaseWorkflow.LEGACY_HELPER_SHA256,
                         hashlib.sha256(helper).hexdigest())
        self.assertEqual(ReleaseWorkflow.LEGACY_MANIFEST_SIZE, len(manifest))
        self.assertEqual(ReleaseWorkflow.LEGACY_MANIFEST_SHA256,
                         hashlib.sha256(manifest).hexdigest())

    def test_consecutive_helper_only_generation_migration(self):
        repository = Path(__file__).resolve().parents[2]
        generation = repository / "fixed-runtime/generation-b9fd468-manifest.json"
        body = generation.read_bytes()
        self.assertEqual(ReleaseWorkflow.CURRENT_GENERATION_MANIFEST_SIZE, len(body))
        self.assertEqual(ReleaseWorkflow.CURRENT_GENERATION_MANIFEST_SHA256,
                         hashlib.sha256(body).hexdigest())
        entries = json.loads(body)["entries"]
        helper = entries["bootstrap/manage-private-config.php"]
        self.assertEqual(64044, helper["size"])
        self.assertEqual(
            "9c00b79ccb81288a6d4bda9983099725a893aab424cbb61304666b7ab8ce44a3",
            helper["sha256"],
        )
        self.assertNotIn(b"<?php", body, "the old helper body must not be bundled")

        old_helper = b"reviewed-old-helper-fixture"
        new_helper = (self.source / "bin/manage-private-config.php").read_bytes()
        target_entries = {
            "bootstrap": {"type": "directory", "mode": 0o700, "size": 0,
                          "sha256": None},
            "bootstrap/manage-private-config.php": {
                "type": "file", "mode": 0o700, "size": len(new_helper),
                "sha256": hashlib.sha256(new_helper).hexdigest(),
            },
        }
        current_entries = json.loads(json.dumps(target_entries))
        current_entries["bootstrap/manage-private-config.php"].update(
            size=len(old_helper), sha256=hashlib.sha256(old_helper).hexdigest())
        manifest_body = (json.dumps({"schema_version": 1, "entries": current_entries},
                                    sort_keys=True, separators=(",", ":")) + "\n").encode()
        manifest_path = Path(self.temp.name) / "current-generation.json"
        manifest_path.write_bytes(manifest_body)
        manifest_path.chmod(0o644)
        self.workflow.CURRENT_GENERATION_MANIFEST_PATH = manifest_path
        self.workflow.CURRENT_GENERATION_MANIFEST_MODE = 0o644
        self.workflow.CURRENT_GENERATION_MANIFEST_SIZE = len(manifest_body)
        self.workflow.CURRENT_GENERATION_MANIFEST_SHA256 = hashlib.sha256(manifest_body).hexdigest()

        remote = self.workflow.PRIVATE_ROOT + "/bootstrap/manage-private-config.php"
        self.deployer.ftps.files[remote] = old_helper
        self.deployer.ftps.modes[remote] = "700"
        self.workflow._migrate_fixed_generation(
            current_entries, target_entries,
            target_expected={remote: (new_helper, "700")},
            expected_hosts=["example.invalid"],
        )
        self.assertEqual(new_helper, self.deployer.ftps.files[remote])
        backup_root = "/private/.fixed-backup-" + self.workflow.CURRENT_GENERATION_MANIFEST_SHA256[:32]
        self.assertEqual(old_helper, self.deployer.ftps.files[backup_root + "/bootstrap/manage-private-config.php"])

    def test_template_filtering_does_not_change_immutable_release_validator(self):
        validator = Path(__file__).resolve().parents[2] / "src/ReleaseValidator.php"
        self.assertEqual(
            "8d73eb0de9782252c0a24ffdec93abd2582d26ed7c1960fc219b7ea34a061821",
            __import__("hashlib").sha256(validator.read_bytes()).hexdigest(),
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        for directory in ("bin", "src", "vendor"):
            (self.source / directory).mkdir(parents=True)
        files = {
            "bin/mail-to-lineworks.php": "<?php echo 'ok';\n",
            "bin/stable-mail-entrypoint.php": "<?php echo 'stable';\n",
            "bin/mail-forward-command-701.php": "<?php\nrequire __DIR__ . '/mail-forward-command.php';\n",
            "bin/validate-release.php": "<?php echo 'validate';\n",
            "bin/manage-private-config.php": "<?php echo 'private-config';\n",
            "src/ReleaseValidator.php": "<?php class ReleaseValidator {}\n",
            "vendor/autoload.php": "<?php return true;\n",
        }
        for relative, body in files.items():
            path = self.source / relative
            path.write_text(body)
            path.chmod(0o700 if relative.startswith("bin/") else 0o600)
        repository = Path(__file__).resolve().parents[2]
        for name in (
            "CanonicalEmail.php", "SystemMailAuthenticator.php", "SendmailClient.php",
            "SendmailProcessAdapter.php", "NativeSendmailProcessAdapter.php",
            "DeliveryHealthMonitor.php", "PrivateStateFilesystem.php",
            "NativePrivateStateFilesystem.php",
        ):
            shutil.copyfile(repository / "src" / name, self.source / "src" / name)
            (self.source / "src" / name).chmod(0o600)
        template = self.source / "vendor/php-di/php-di/src/Compiler/Template.php"
        template.parent.mkdir(parents=True)
        template.write_text("/** generated */\nclass <?php echo $className; ?>\n")
        template.chmod(0o600)
        self.deployer = FakeReleaseDeployer()
        self.workflow = ReleaseWorkflow(
            self.deployer, "/home/example", "/home/example/private/config.json"
        )

    def _seed_legacy_fixed_runtime(self, *, prefix=0, helper_family="new"):
        repository = Path(__file__).resolve().parents[2]
        shutil.copyfile(repository / "bin/manage-private-config.php",
                        self.source / "bin/manage-private-config.php")
        (self.source / "bin/manage-private-config.php").chmod(0o700)
        self.workflow.provision_fixed_runtime(self.source)
        full_entries = next(entries for root, entries, _hosts
                            in self.deployer.remote_validator.inspections
                            if root == "/home/example/private/xserver-mail-lineworks")
        legacy_entries = json.loads(json.dumps(full_entries))
        helper = legacy_entries.pop("bootstrap/manage-private-config.php")
        old_bodies = {}
        for index, relative in enumerate(self.MIGRATION_ORDER):
            old = ("legacy-%d:" % index).encode() + relative.encode()
            old_bodies[relative] = old
            legacy_entries[relative] = dict(legacy_entries[relative], size=len(old),
                                             sha256=hashlib.sha256(old).hexdigest())
        manifest_body = (json.dumps({"schema_version": 1, "entries": legacy_entries},
                                    sort_keys=True, separators=(",", ":")) + "\n").encode()
        asset = Path(self.temp.name) / "legacy-manifest.json"
        asset.write_bytes(manifest_body); asset.chmod(0o644)
        self.workflow.LEGACY_MANIFEST_PATH = asset
        self.workflow.LEGACY_MANIFEST_MODE = 0o644
        self.workflow.LEGACY_MANIFEST_SIZE = len(manifest_body)
        self.workflow.LEGACY_MANIFEST_SHA256 = hashlib.sha256(manifest_body).hexdigest()
        current_generation = json.loads(json.dumps(full_entries))
        old_helper = b"reviewed-old-helper-fixture"
        current_generation["bootstrap/manage-private-config.php"].update(
            size=len(old_helper), sha256=hashlib.sha256(old_helper).hexdigest())
        current_body = (json.dumps({"schema_version": 1, "entries": current_generation},
                                   sort_keys=True, separators=(",", ":")) + "\n").encode()
        current_asset = Path(self.temp.name) / "current-generation-manifest.json"
        current_asset.write_bytes(current_body); current_asset.chmod(0o644)
        self.workflow.CURRENT_GENERATION_MANIFEST_PATH = current_asset
        self.workflow.CURRENT_GENERATION_MANIFEST_MODE = 0o644
        self.workflow.CURRENT_GENERATION_MANIFEST_SIZE = len(current_body)
        self.workflow.CURRENT_GENERATION_MANIFEST_SHA256 = hashlib.sha256(current_body).hexdigest()
        for index, relative in enumerate(self.MIGRATION_ORDER):
            remote = self.workflow.PRIVATE_ROOT + "/" + relative
            if index >= prefix:
                self.deployer.ftps.files[remote] = old_bodies[relative]
                self.deployer.ftps.modes[remote] = format(legacy_entries[relative]["mode"], "03o")
        if helper_family == "old":
            helper_remote = self.workflow.PRIVATE_ROOT + "/bootstrap/manage-private-config.php"
            self.deployer.ftps.files[helper_remote] = old_helper
            self.deployer.ftps.modes[helper_remote] = "700"
        self.deployer.ftps.deployments.clear()
        self.deployer.ftps.events.clear()
        self.deployer.ftps.verifications.clear()
        self.deployer.remote_validator.inspections.clear()
        return legacy_entries, helper, old_bodies

    def test_every_old_and_new_helper_three_file_prefix_reaches_final_generation(self):
        for family in ("old", "new"):
            for prefix in range(4):
                with self.subTest(family=family, prefix=prefix):
                    self.deployer = FakeReleaseDeployer()
                    self.workflow = ReleaseWorkflow(
                        self.deployer, "/home/example", "/home/example/private/config.json")
                    legacy, _helper, old_bodies = self._seed_legacy_fixed_runtime(
                        prefix=prefix, helper_family=family)
                    if prefix and not (family == "old" and prefix == 3):
                        generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
                        for relative, body in old_bodies.items():
                            remote = "/private/.fixed-backup-" + generation + "/" + relative
                            self.deployer.ftps.files[remote] = body
                            self.deployer.ftps.modes[remote] = format(
                                legacy[relative]["mode"], "03o")
                    self.workflow.provision_fixed_runtime(self.source)
                    helper_remote = (self.workflow.PRIVATE_ROOT
                                     + "/bootstrap/manage-private-config.php")
                    self.assertEqual((self.source / "bin/manage-private-config.php").read_bytes(),
                                     self.deployer.ftps.files[helper_remote])

    def test_second_generation_crash_resume_and_trust_boundaries(self):
        class InjectedAbort(BaseException):
            pass

        for boundary in ("before-stage", "after-stage", "before-publish",
                         "after-publish", "after-helper-replace", "after-final-readback"):
            with self.subTest(boundary=boundary):
                self.deployer = FakeReleaseDeployer()
                self.workflow = ReleaseWorkflow(
                    self.deployer, "/home/example", "/home/example/private/config.json")
                legacy, _helper, old_bodies = self._seed_legacy_fixed_runtime(
                    prefix=3, helper_family="old")
                generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
                for relative, body in old_bodies.items():
                    remote = "/private/.fixed-backup-" + generation + "/" + relative
                    self.deployer.ftps.files[remote] = body
                    self.deployer.ftps.modes[remote] = format(legacy[relative]["mode"], "03o")

                def abort(*_args):
                    raise InjectedAbort()
                if boundary == "before-stage":
                    self.deployer.ftps.before_deploy = abort
                elif boundary == "after-stage":
                    self.deployer.ftps.after_deploy = abort
                elif boundary == "before-publish":
                    self.deployer.ftps.before_publish = abort
                elif boundary == "after-publish":
                    self.deployer.ftps.after_publish = abort
                else:
                    if boundary == "after-helper-replace":
                        self.deployer.ftps.after_replace = abort
                    else:
                        final_sha = hashlib.sha256(
                            (self.source / "bin/manage-private-config.php").read_bytes()
                        ).hexdigest()
                        def abort_final(root, entries, state):
                            if (root == "/home/example/private/xserver-mail-lineworks"
                                    and state == "EXACT"
                                    and entries["bootstrap/manage-private-config.php"]["sha256"]
                                    == final_sha):
                                raise InjectedAbort()
                        self.deployer.remote_validator.after_inspect = abort_final
                with self.assertRaises(InjectedAbort):
                    self.workflow.provision_fixed_runtime(self.source)
                self.deployer.ftps.before_deploy = None
                self.deployer.ftps.after_deploy = None
                self.deployer.ftps.before_publish = None
                self.deployer.ftps.after_publish = None
                self.deployer.ftps.after_replace = None
                self.deployer.remote_validator.after_inspect = None
                self.workflow.provision_fixed_runtime(self.source)
                helper_remote = (self.workflow.PRIVATE_ROOT
                                 + "/bootstrap/manage-private-config.php")
                self.assertEqual((self.source / "bin/manage-private-config.php").read_bytes(),
                                 self.deployer.ftps.files[helper_remote])

        for tamper in ("backup", "live-helper", "unrelated-fixed", "ambiguous-result"):
            with self.subTest(tamper=tamper):
                self.deployer = FakeReleaseDeployer()
                self.workflow = ReleaseWorkflow(
                    self.deployer, "/home/example", "/home/example/private/config.json")
                legacy, _helper, old_bodies = self._seed_legacy_fixed_runtime(
                    prefix=3, helper_family="old")
                generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
                for relative, body in old_bodies.items():
                    remote = "/private/.fixed-backup-" + generation + "/" + relative
                    self.deployer.ftps.files[remote] = body
                    self.deployer.ftps.modes[remote] = format(legacy[relative]["mode"], "03o")
                helper_remote = (self.workflow.PRIVATE_ROOT
                                 + "/bootstrap/manage-private-config.php")
                before_events = list(self.deployer.ftps.events)
                if tamper == "unrelated-fixed":
                    unrelated = self.workflow.PRIVATE_ROOT + "/vendor/autoload.php"
                    self.deployer.ftps.files[unrelated] = b"changed unrelated fixed file"
                elif tamper == "ambiguous-result":
                    original = self.deployer.ftps.replace_bytes_atomic
                    def unknown_then_raise(remote, _body, *, mode="600"):
                        return original(remote, b"unknown helper", mode=mode)
                    self.deployer.ftps.replace_bytes_atomic = unknown_then_raise
                else:
                    changed = False
                    generation_backup = ("/private/.fixed-backup-"
                                         + self.workflow.CURRENT_GENERATION_MANIFEST_SHA256[:32]
                                         + "/bootstrap/manage-private-config.php")
                    backup_filesystem = "/home/example" + generation_backup.rsplit("/", 2)[0]
                    def mutate(root, _entries, state):
                        nonlocal changed
                        if not changed and root == backup_filesystem and state == "EXACT":
                            changed = True
                            target = generation_backup if tamper == "backup" else helper_remote
                            self.deployer.ftps.files[target] = b"tampered"
                    self.deployer.remote_validator.after_inspect = mutate
                with self.assertRaises((ReleaseWorkflowError, RemoteValidationError)):
                    self.workflow.provision_fixed_runtime(self.source)
                self.assertFalse(any(event[0] == "replace"
                                     for event in self.deployer.ftps.events[len(before_events):]
                                     if tamper != "ambiguous-result"))

    def test_generation_manifest_stable_descriptor_rejects_tamper_mode_owner_and_identity(self):
        repository = Path(__file__).resolve().parents[2]
        source = repository / "fixed-runtime/generation-b9fd468-manifest.json"
        expected = dict(
            expected_mode=0o644,
            expected_size=ReleaseWorkflow.CURRENT_GENERATION_MANIFEST_SIZE,
            expected_sha256=ReleaseWorkflow.CURRENT_GENERATION_MANIFEST_SHA256,
        )
        self.assertEqual(source.read_bytes(), ReleaseWorkflow._read_pinned_asset(source, **expected))
        with self.assertRaises(ReleaseWorkflowError):
            ReleaseWorkflow._read_pinned_asset(source, expected_uid=os.getuid() + 1, **expected)
        replacement = Path(self.temp.name) / "generation-replacement.json"
        shutil.copyfile(source, replacement); replacement.chmod(0o644)
        with self.assertRaises(ReleaseWorkflowError):
            ReleaseWorkflow._read_pinned_asset(
                source, open_fn=lambda _path, flags: os.open(replacement, flags), **expected)
        changed = Path(self.temp.name) / "generation-changed.json"
        changed.write_bytes(source.read_bytes() + b" "); changed.chmod(0o644)
        with self.assertRaises(ReleaseWorkflowError):
            ReleaseWorkflow._read_pinned_asset(
                changed, expected_mode=0o644, expected_size=changed.stat().st_size,
                expected_sha256=ReleaseWorkflow.CURRENT_GENERATION_MANIFEST_SHA256)
        changed.chmod(0o600)
        with self.assertRaises(ReleaseWorkflowError):
            ReleaseWorkflow._read_pinned_asset(changed, **expected)

    def test_second_generation_ambiguous_replace_accepts_reviewed_old_helper_and_retries(self):
        self.deployer = FakeReleaseDeployer()
        self.workflow = ReleaseWorkflow(
            self.deployer, "/home/example", "/home/example/private/config.json")
        self._seed_legacy_fixed_runtime(prefix=3, helper_family="old")
        original = self.deployer.ftps.replace_bytes_atomic
        calls = 0
        def ambiguous_old(remote, body, *, mode="600"):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("rename result unavailable while old bytes remain")
            return original(remote, body, mode=mode)
        self.deployer.ftps.replace_bytes_atomic = ambiguous_old
        self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual(2, calls)

    def test_ambiguous_old_helper_revalidates_live_and_backup_before_retry(self):
        for tamper in ("backup", "live-ftps"):
            with self.subTest(tamper=tamper):
                self.deployer = FakeReleaseDeployer()
                self.workflow = ReleaseWorkflow(
                    self.deployer, "/home/example", "/home/example/private/config.json")
                self._seed_legacy_fixed_runtime(prefix=3, helper_family="old")
                original = self.deployer.ftps.replace_bytes_atomic
                calls = 0

                def ambiguous_old(remote, body, *, mode="600"):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        if tamper == "backup":
                            backup = ("/private/.fixed-backup-"
                                      + self.workflow.CURRENT_GENERATION_MANIFEST_SHA256[:32]
                                      + "/bootstrap/manage-private-config.php")
                            self.deployer.ftps.files[backup] = b"tampered after ambiguity"
                        else:
                            real_verify = self.deployer.ftps.verify_private_file_hashes
                            def reject_live(expected):
                                if remote in expected:
                                    raise RuntimeError("live FTPS trust changed")
                                return real_verify(expected)
                            self.deployer.ftps.verify_private_file_hashes = reject_live
                        raise RuntimeError("rename result unavailable while old bytes remain")
                    return original(remote, body, mode=mode)

                self.deployer.ftps.replace_bytes_atomic = ambiguous_old
                with self.assertRaises(ReleaseWorkflowError):
                    self.workflow.provision_fixed_runtime(self.source)
                self.assertEqual(1, calls, "trust drift must stop before retry mutation")

    def test_shared_transaction_validates_complete_live_and_backup_for_both_migrations(self):
        for family, prefix, generation_attribute in (
                ("new", 0, "LEGACY_MANIFEST_SHA256"),
                ("old", 3, "CURRENT_GENERATION_MANIFEST_SHA256")):
            with self.subTest(family=family):
                self.deployer = FakeReleaseDeployer()
                self.workflow = ReleaseWorkflow(
                    self.deployer, "/home/example", "/home/example/private/config.json")
                self._seed_legacy_fixed_runtime(prefix=prefix, helper_family=family)
                self.workflow.provision_fixed_runtime(self.source)
                live = frozenset(
                    path for path in self.deployer.ftps.files
                    if path.startswith(self.workflow.PRIVATE_ROOT + "/")
                    and not any(segment in path for segment in (
                        "/deploy-transactions/", "/logs/", "/releases/", "/state/"))
                )
                generation = getattr(self.workflow, generation_attribute)[:32]
                backup = frozenset(
                    path for path in self.deployer.ftps.files
                    if path.startswith("/private/.fixed-backup-" + generation + "/")
                )
                self.assertIn(live, self.deployer.ftps.hash_verifications)
                self.assertIn(backup, self.deployer.ftps.hash_verifications)

    def test_partial_staging_cleanup_requires_exact_ftps_subset_and_absent_backup(self):
        for transaction in ("legacy", "generation"):
            for drift in ("safe", "bytes", "mode", "unknown", "backup",
                          "ssh-subset", "ftps-error"):
                with self.subTest(transaction=transaction, drift=drift):
                    self.deployer = FakeReleaseDeployer()
                    self.workflow = ReleaseWorkflow(
                        self.deployer, "/home/example", "/home/example/private/config.json")
                    if transaction == "legacy":
                        legacy, _helper, old_bodies = self._seed_legacy_fixed_runtime(
                            prefix=0, helper_family="new")
                        migration_order = self.MIGRATION_ORDER
                        generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
                        first = migration_order[0]
                        first_body = old_bodies[first]
                        first_mode = format(legacy[first]["mode"], "03o")
                        run = lambda: self.workflow.provision_fixed_runtime(self.source)
                    else:
                        migration_order = (
                            "bootstrap/manage-private-config.php",
                            "bootstrap/validate-release.php",
                        )
                        old_bodies = {
                            migration_order[0]: b"reviewed-old-helper",
                            migration_order[1]: b"reviewed-old-validator",
                        }
                        new_bodies = {
                            migration_order[0]: b"reviewed-new-helper",
                            migration_order[1]: b"reviewed-new-validator",
                        }
                        current = {"bootstrap": {
                            "type": "directory", "mode": 0o700, "size": 0,
                            "sha256": None,
                        }}
                        target = json.loads(json.dumps(current))
                        target_expected = {}
                        for relative in migration_order:
                            current[relative] = {
                                "type": "file", "mode": 0o700,
                                "size": len(old_bodies[relative]),
                                "sha256": hashlib.sha256(old_bodies[relative]).hexdigest(),
                            }
                            target[relative] = {
                                "type": "file", "mode": 0o700,
                                "size": len(new_bodies[relative]),
                                "sha256": hashlib.sha256(new_bodies[relative]).hexdigest(),
                            }
                            remote = self.workflow.PRIVATE_ROOT + "/" + relative
                            self.deployer.ftps.files[remote] = old_bodies[relative]
                            self.deployer.ftps.modes[remote] = "700"
                            target_expected[remote] = (new_bodies[relative], "700")
                        self.workflow.CURRENT_GENERATION_MANIFEST_SHA256 = "c" * 64
                        generation = "c" * 32
                        first = migration_order[0]
                        first_body = old_bodies[first]
                        first_mode = "700"
                        run = lambda: self.workflow._migrate_fixed_generation(
                            current, target, migration_order=migration_order,
                            target_expected=target_expected,
                            expected_hosts=["example.invalid"],
                        )

                    staging_root = "/private/.fixed-backup-staging-" + generation
                    staging_filesystem = "/home/example" + staging_root
                    backup_root = "/private/.fixed-backup-" + generation
                    staging_file = staging_root + "/" + first
                    self.deployer.ftps.files[staging_file] = first_body
                    self.deployer.ftps.modes[staging_file] = first_mode
                    changed = False

                    def drift_after_partial(root, _entries, state):
                        nonlocal changed
                        if changed or root != staging_filesystem or state != "PARTIAL":
                            return
                        changed = True
                        if drift == "bytes":
                            self.deployer.ftps.files[staging_file] = b"tampered staging"
                        elif drift == "mode":
                            self.deployer.ftps.modes[staging_file] = (
                                "600" if first_mode != "600" else "700")
                        elif drift == "unknown":
                            unknown = staging_root + "/bootstrap/unknown.php"
                            self.deployer.ftps.files[unknown] = b"unknown"
                            self.deployer.ftps.modes[unknown] = "700"
                        elif drift == "backup":
                            appeared = backup_root + "/" + first
                            self.deployer.ftps.files[appeared] = first_body
                            self.deployer.ftps.modes[appeared] = first_mode

                    if drift != "safe":
                        self.deployer.remote_validator.after_inspect = drift_after_partial
                    if drift == "ssh-subset":
                        absent = staging_root + "/" + migration_order[1]
                        self.deployer.remote_validator.details_override = (
                            lambda root, details: {
                                **details,
                                "present_files": tuple(sorted(
                                    set(details["present_files"])
                                    | {migration_order[1]})),
                            } if root == staging_filesystem else details
                        )
                        self.assertNotIn(absent, self.deployer.ftps.files)
                    elif drift == "ftps-error":
                        self.deployer.ftps.subset_error = RuntimeError(
                            "remote file could not be verified")
                    if drift == "safe":
                        run()
                        self.assertIn(staging_root, self.deployer.ftps.cleanups)
                    else:
                        expected_error = ("backup stagingのFTPS検証に失敗"
                                          if drift in {"ssh-subset", "ftps-error"} else None)
                        context = (self.assertRaisesRegex(ReleaseWorkflowError, expected_error)
                                   if expected_error else self.assertRaises(ReleaseWorkflowError))
                        with context:
                            run()
                        self.assertEqual([], self.deployer.ftps.cleanups,
                                         "untrusted partial staging must not be deleted")
                        self.assertFalse(any(event[0] in {"deploy", "publish", "replace"}
                                             for event in self.deployer.ftps.events))

    def test_migrates_exact_legacy_through_three_atomic_prefixes_after_verified_backup(self):
        _legacy, _helper, old_bodies = self._seed_legacy_fixed_runtime()
        untouched = {
            "/private/config.json": b"config",
            "/private/xserver-mail-lineworks/state/dedup/message-id": b"dedup",
            "/private/xserver-mail-lineworks/logs/events.jsonl": b"log",
            "/public_html/index.html": b"public",
            "/mailbox/inbox": b"mail",
        }
        self.deployer.ftps.files.update(untouched)
        self.deployer.ftps.modes.update({path: "600" for path in untouched})

        self.workflow.provision_fixed_runtime(self.source)

        replacements = [event[1] for event in self.deployer.ftps.events
                        if event[0] == "replace"]
        self.assertEqual([self.workflow.PRIVATE_ROOT + "/" + path
                          for path in self.MIGRATION_ORDER], replacements)
        backup_publish = next(index for index, event in enumerate(self.deployer.ftps.events)
                              if event[0] == "publish" and ".fixed-backup-" in event[2])
        first_replace = next(index for index, event in enumerate(self.deployer.ftps.events)
                             if event[0] == "replace")
        self.assertLess(backup_publish, first_replace)
        for relative, old in old_bodies.items():
            matches = [body for path, body in self.deployer.ftps.files.items()
                       if ".fixed-backup-" in path and path.endswith("/" + relative)]
            self.assertEqual([old], matches)
        self.assertEqual(untouched, {path: self.deployer.ftps.files[path] for path in untouched})
        self.assertFalse(hasattr(self.deployer, "api"))

    def test_migration_resumes_after_process_abort_at_each_atomic_replacement(self):
        class InjectedAbort(BaseException):
            pass

        for stop_after in (1, 2, 3):
            with self.subTest(stop_after=stop_after):
                self.deployer = FakeReleaseDeployer()
                self.workflow = ReleaseWorkflow(
                    self.deployer, "/home/example", "/home/example/private/config.json")
                self._seed_legacy_fixed_runtime()
                count = 0
                def abort(_path):
                    nonlocal count
                    count += 1
                    if count == stop_after:
                        raise InjectedAbort()
                self.deployer.ftps.after_replace = abort
                with self.assertRaises(InjectedAbort):
                    self.workflow.provision_fixed_runtime(self.source)
                self.deployer.ftps.after_replace = None
                self.workflow.provision_fixed_runtime(self.source)
                final_entries = next(entries for root, entries, _hosts
                                     in reversed(self.deployer.remote_validator.inspections)
                                     if root == "/home/example/private/xserver-mail-lineworks")
                self.assertEqual("EXACT", self.deployer.remote_validator.inspect_fixed_runtime(
                    "/home/example/private/xserver-mail-lineworks", final_entries,
                    expected_hosts=["example.xsrv.jp"]))

    def test_migration_revalidates_complete_backup_before_each_live_replacement(self):
        for tamper in ("other-file", "directory"):
            with self.subTest(tamper=tamper):
                self.deployer = FakeReleaseDeployer()
                self.workflow = ReleaseWorkflow(
                    self.deployer, "/home/example", "/home/example/private/config.json")
                self._seed_legacy_fixed_runtime()
                generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
                backup_root = "/private/.fixed-backup-" + generation
                backup_filesystem = "/home/example" + backup_root
                replacements = 0

                def tamper_after_first(_path):
                    nonlocal replacements
                    replacements += 1
                    if replacements != 1:
                        return
                    if tamper == "other-file":
                        target = backup_root + "/bootstrap/mail-forward-command.php"
                        self.deployer.ftps.files[target] = b"tampered backup"
                    else:
                        self.deployer.remote_validator.invalid_directories.add(
                            (backup_filesystem, "bootstrap"))

                self.deployer.ftps.after_replace = tamper_after_first
                with self.assertRaises(ReleaseWorkflowError):
                    self.workflow.provision_fixed_runtime(self.source)
                live_replacements = [event for event in self.deployer.ftps.events
                                     if event[0] == "replace"]
                self.assertEqual(1, len(live_replacements),
                                 "backup tamper must stop before the next live replace")

    def test_migration_rechecks_whole_live_prefix_immediately_before_each_replacement(self):
        self._seed_legacy_fixed_runtime()
        filesystem_root = "/home/example/private/xserver-mail-lineworks"
        generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
        backup_filesystem = "/home/example/private/.fixed-backup-" + generation
        tampered = False

        def tamper_after_backup_check(root, _entries, state):
            nonlocal tampered
            replacements = [event for event in self.deployer.ftps.events
                            if event[0] == "replace"]
            if (not tampered and len(replacements) == 1
                    and root == backup_filesystem and state == "EXACT"):
                tampered = True
                other = self.workflow.PRIVATE_ROOT + "/" + self.MIGRATION_ORDER[2]
                self.deployer.ftps.files[other] = b"changed between prefix checks"

        self.deployer.remote_validator.after_inspect = tamper_after_backup_check
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)

        self.assertTrue(tampered)
        live_replacements = [event for event in self.deployer.ftps.events
                             if event[0] == "replace"]
        self.assertEqual(1, len(live_replacements),
                         "live tamper must stop before the next replacement")
        live_inspections = [entries for root, entries, _hosts
                            in self.deployer.remote_validator.inspections
                            if root == filesystem_root]
        self.assertGreaterEqual(len(live_inspections), 1)

    def test_migration_accepts_each_allowed_prefix_and_resolves_ambiguous_rename_by_readback(self):
        for prefix in (0, 1, 2):
            with self.subTest(prefix=prefix):
                self.deployer = FakeReleaseDeployer()
                self.workflow = ReleaseWorkflow(
                    self.deployer, "/home/example", "/home/example/private/config.json")
                legacy, _helper, old_bodies = self._seed_legacy_fixed_runtime(prefix=prefix)
                if prefix:
                    generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
                    for relative, body in old_bodies.items():
                        remote = "/private/.fixed-backup-" + generation + "/" + relative
                        self.deployer.ftps.files[remote] = body
                        self.deployer.ftps.modes[remote] = format(legacy[relative]["mode"], "03o")
                self.deployer.ftps.raise_after_replace = True
                self.workflow.provision_fixed_runtime(self.source)
                self.assertTrue(any(
                    root == "/home/example/private/xserver-mail-lineworks"
                    for root, _relative in self.deployer.remote_validator.strict_mismatches
                ), "helper-present migration must enter through the initial strict mismatch")

    def test_migration_rejects_non_prefix_and_partial_backup_without_live_mutation(self):
        self._seed_legacy_fixed_runtime()
        first = self.workflow.PRIVATE_ROOT + "/" + self.MIGRATION_ORDER[0]
        second = self.workflow.PRIVATE_ROOT + "/" + self.MIGRATION_ORDER[1]
        self.deployer.ftps.files[second] = (self.source / "bin/validate-release.php").read_bytes()
        before = dict(self.deployer.ftps.files)
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual(before, self.deployer.ftps.files)
        self.assertFalse(any(event[0] in {"deploy", "publish", "replace"}
                             for event in self.deployer.ftps.events))
        self.assertEqual([], self.deployer.ftps.cleanups)

        self.deployer = FakeReleaseDeployer()
        self.workflow = ReleaseWorkflow(
            self.deployer, "/home/example", "/home/example/private/config.json")
        self._seed_legacy_fixed_runtime(prefix=1)
        generation = self.workflow.LEGACY_MANIFEST_SHA256[:32]
        partial = "/private/.fixed-backup-" + generation + "/src/ReleaseValidator.php"
        self.deployer.ftps.files[partial] = b"partial"
        self.deployer.ftps.modes[partial] = "600"
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)
        self.assertFalse(any(event[0] == "replace" for event in self.deployer.ftps.events))

    def test_production_stage_generates_manifests_provisions_dependencies_and_uses_filesystem_path(self):
        staged = self.workflow.stage(self.source, "release-abc")
        self.assertEqual("/private/xserver-mail-lineworks/releases/release-abc",
                         self.deployer.stages[0][0])
        self.assertEqual("/home/example/private/xserver-mail-lineworks/releases/release-abc",
                         self.deployer.stages[0][1])
        stable = self.deployer.stages[0][2]
        self.assertEqual("bin/mail-to-lineworks.php", stable["entrypoint"]["path"])
        self.assertNotIn(
            "vendor/php-di/php-di/src/Compiler/Template.php",
            [item["path"] for item in stable["runtime"]],
        )
        runtime = {item["path"]: item for item in stable["runtime"]}
        for name in (
            "CanonicalEmail.php", "SystemMailAuthenticator.php", "SendmailClient.php",
            "SendmailProcessAdapter.php", "NativeSendmailProcessAdapter.php",
            "DeliveryHealthMonitor.php", "PrivateStateFilesystem.php",
            "NativePrivateStateFilesystem.php",
        ):
            with self.subTest(runtime=name):
                relative = "src/" + name
                source = self.source / relative
                self.assertIn(relative, runtime)
                self.assertEqual(0o600, runtime[relative]["mode"])
                self.assertEqual(source.stat().st_size, runtime[relative]["size"])
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                                 runtime[relative]["sha256"])
        self.assertTrue(any(path.startswith("/private/.fixed-staging-")
                            for path in self.deployer.ftps.deployments))
        self.assertRegex(staged["manifest_sha256"], r"^[a-f0-9]{64}$")
        self.assertFalse(hasattr(self.deployer, "api"), "stage must not require or mutate API")

        wrapper = "/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php"
        self.assertEqual(
            b"<?php\nrequire __DIR__ . '/mail-forward-command.php';\n",
            self.deployer.ftps.files[wrapper],
        )
        self.assertEqual("701", self.deployer.ftps.modes[wrapper])
        fixed_entries = self.deployer.ftps.verifications[0][0]
        self.assertEqual("701", fixed_entries[wrapper][1])
        helper = "/private/xserver-mail-lineworks/bootstrap/manage-private-config.php"
        self.assertEqual((self.source / "bin/manage-private-config.php").read_bytes(),
                         self.deployer.ftps.files[helper])
        self.assertEqual("700", self.deployer.ftps.modes[helper])
        helper_entry = fixed_entries[helper]
        self.assertEqual("700", helper_entry[1])
        ssh_entries = self.deployer.remote_validator.inspections[0][1]
        self.assertEqual(0o700, ssh_entries["bootstrap/manage-private-config.php"]["mode"])
        self.assertEqual(__import__("hashlib").sha256(
            (self.source / "bin/manage-private-config.php").read_bytes()
        ).hexdigest(), ssh_entries["bootstrap/manage-private-config.php"]["sha256"])

    def test_existing_identical_fixed_runtime_is_not_reuploaded_and_locator_is_exact(self):
        self.workflow.provision_fixed_runtime(self.source)
        deployments = list(self.deployer.ftps.deployments)
        staged = self.workflow.stage(self.source, "release-abc")
        self.assertEqual(deployments, self.deployer.ftps.deployments[:1])
        self.assertEqual("digest", self.workflow.switch(staged))
        locator = self.deployer.switches[0][1]
        self.assertEqual("/home/example/private/config.json", locator["config_path"])
        self.assertEqual(staged["manifest_sha256"], locator["manifest_sha256"])

    def test_legacy_exact_fixed_runtime_adds_only_private_config_helper_atomically(self):
        helper = "/private/xserver-mail-lineworks/bootstrap/manage-private-config.php"
        helper_bytes = (self.source / "bin/manage-private-config.php").read_bytes()
        self.workflow.provision_fixed_runtime(self.source)
        del self.deployer.ftps.files[helper]
        del self.deployer.ftps.modes[helper]
        deployments = list(self.deployer.ftps.deployments)

        self.workflow.provision_fixed_runtime(self.source)

        self.assertEqual(deployments, self.deployer.ftps.deployments)
        self.assertEqual(helper_bytes, self.deployer.ftps.files[helper])
        self.assertEqual("700", self.deployer.ftps.modes[helper])

    def test_bundled_legacy_assets_bootstrap_over_ssh_when_fixed_ftps_write_is_denied(self):
        self.workflow.provision_fixed_runtime(self.source)
        helper_remote = "/private/xserver-mail-lineworks/bootstrap/manage-private-config.php"
        del self.deployer.ftps.files[helper_remote]
        del self.deployer.ftps.modes[helper_remote]
        full_entries = self.deployer.remote_validator.inspections[0][1]
        legacy_entries = {key: value for key, value in full_entries.items()
                          if key != "bootstrap/manage-private-config.php"}
        helper, manifest = self._copy_tracked_legacy_assets()
        helper_body = helper.read_bytes()
        pinned_entries = json.loads(manifest.read_text())["entries"]
        def denied_legacy_retr(_expected):
            raise error_perm("550 RETR denied")
        self.deployer.ftps.verify_private_file_hashes = denied_legacy_retr
        def denied_helper_write(*_args, **_kwargs):
            raise error_perm("550 helper temp write denied")
        self.deployer.ftps.replace_bytes_atomic = denied_helper_write
        class SequenceValidator:
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
        self.deployer.remote_validator = SequenceValidator()
        deployments = list(self.deployer.ftps.deployments)
        before_files = dict(self.deployer.ftps.files)

        self.assertTrue(self.workflow.provision_legacy_helper_assets(
            helper, manifest, expected_mode=0o644))
        self.assertEqual(deployments, self.deployer.ftps.deployments)
        self.assertEqual(before_files, self.deployer.ftps.files)
        self.assertNotIn(helper_remote, self.deployer.ftps.files)
        self.assertEqual([(
            "/home/example/private/xserver-mail-lineworks",
            "bootstrap/manage-private-config.php", helper_body,
            hashlib.sha256(helper_body).hexdigest(), 0o700, ["example.xsrv.jp"],
        )], self.deployer.remote_validator.provisions)
        self.assertEqual(pinned_entries, self.deployer.remote_validator.inspections[1][1])

    def test_bundled_bootstrap_general_ssh_failure_never_writes_ftps(self):
        helper, manifest = self._copy_tracked_legacy_assets("assets-failure")
        self.workflow.provision_fixed_runtime(self.source)
        before = dict(self.deployer.ftps.files)
        class FailingValidator:
            def inspect_fixed_runtime(self, *_args, **_kwargs):
                raise RuntimeError("general SSH failure")
        self.deployer.remote_validator = FailingValidator()
        with self.assertRaises(RuntimeError):
            self.workflow.provision_legacy_helper_assets(
                helper, manifest, expected_mode=0o644)
        self.assertEqual(before, self.deployer.ftps.files)

    def test_legacy_assets_reject_tamper_symlink_and_wrong_mode_before_remote_writes(self):
        variants = ("helper-tamper", "manifest-tamper", "helper-symlink", "wrong-mode")
        for variant in variants:
            with self.subTest(variant=variant):
                helper, manifest = self._copy_tracked_legacy_assets("assets-" + variant)
                if variant == "helper-tamper":
                    helper.write_bytes(helper.read_bytes() + b"\n")
                elif variant == "manifest-tamper":
                    body = bytearray(manifest.read_bytes())
                    body[-2] ^= 1
                    manifest.write_bytes(body)
                elif variant == "helper-symlink":
                    target = helper.with_suffix(".real")
                    helper.rename(target)
                    helper.symlink_to(target)
                else:
                    helper.chmod(0o600)
                before = dict(self.deployer.ftps.files)
                inspections = len(self.deployer.remote_validator.inspections)
                with self.assertRaises(ReleaseWorkflowError):
                    self.workflow.provision_legacy_helper_assets(
                        helper, manifest, expected_mode=0o644)
                self.assertEqual(before, self.deployer.ftps.files)
                self.assertEqual(inspections, len(self.deployer.remote_validator.inspections))

    def test_pinned_asset_reader_rejects_owner_and_lstat_open_identity_changes(self):
        helper, _manifest = self._copy_tracked_legacy_assets("identity-assets")
        real_lstat = os.lstat(helper)
        wrong_owner = SimpleNamespace(**{
            name: getattr(real_lstat, name) for name in (
                "st_mode", "st_uid", "st_dev", "st_ino", "st_size",
            )
        })
        wrong_owner.st_uid += 1
        with self.assertRaises(ReleaseWorkflowError):
            ReleaseWorkflow._read_pinned_asset(
                helper, expected_mode=0o644,
                expected_size=ReleaseWorkflow.LEGACY_HELPER_SIZE,
                expected_sha256=ReleaseWorkflow.LEGACY_HELPER_SHA256,
                expected_uid=os.getuid(), lstat_fn=lambda _path: wrong_owner)

        replacement = helper.with_suffix(".replacement")
        shutil.copyfile(helper, replacement)
        replacement.chmod(0o644)
        with self.assertRaises(ReleaseWorkflowError):
            ReleaseWorkflow._read_pinned_asset(
                helper, expected_mode=0o644,
                expected_size=ReleaseWorkflow.LEGACY_HELPER_SIZE,
                expected_sha256=ReleaseWorkflow.LEGACY_HELPER_SHA256,
                expected_uid=os.getuid(),
                open_fn=lambda _path, flags: os.open(replacement, flags))

        fstat_calls = 0
        def changed_after_read(fd):
            nonlocal fstat_calls
            fstat_calls += 1
            current = os.fstat(fd)
            if fstat_calls == 1:
                return current
            changed = SimpleNamespace(**{
                name: getattr(current, name) for name in (
                    "st_mode", "st_uid", "st_dev", "st_ino", "st_size",
                )
            })
            changed.st_ino += 1
            return changed
        with self.assertRaises(ReleaseWorkflowError):
            ReleaseWorkflow._read_pinned_asset(
                helper, expected_mode=0o644,
                expected_size=ReleaseWorkflow.LEGACY_HELPER_SIZE,
                expected_sha256=ReleaseWorkflow.LEGACY_HELPER_SHA256,
                expected_uid=os.getuid(), fstat_fn=changed_after_read)

    def test_fixed_runtime_uses_batch_ftps_verification_for_existing_and_new_trees(self):
        def forbid_legacy(*_args, **_kwargs):
            raise AssertionError("per-file FTPS verification must not be used")

        self.deployer.ftps.read_optional_bytes = forbid_legacy
        self.deployer.ftps.read_bytes = forbid_legacy
        self.deployer.ftps.assert_file_mode = forbid_legacy
        self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual([True, False, False], [
            allow_all_missing for _expected, allow_all_missing
            in self.deployer.ftps.verifications
        ])
        first_count = len(self.deployer.ftps.verifications)
        self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual(first_count + 2, len(self.deployer.ftps.verifications))
        self.assertTrue(self.deployer.ftps.verifications[-2][1])
        self.assertFalse(self.deployer.ftps.verifications[-1][1])

    def test_rejects_relative_source_and_symlinked_fixed_dependency_before_deploy(self):
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.stage(Path("relative"), "release-abc")
        stable = self.source / "bin/stable-mail-entrypoint.php"
        stable.unlink()
        stable.symlink_to(self.source / "vendor/autoload.php")
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.stage(self.source, "release-abc")
        self.assertEqual([], self.deployer.ftps.deployments)

    def test_wrapper_source_must_match_code_owned_bytes_before_any_remote_call(self):
        wrapper = self.source / "bin/mail-forward-command-701.php"
        variants = (
            b"<?php\n require __DIR__ . '/mail-forward-command.php';\n",
            b"<?php\ninclude __DIR__ . '/mail-forward-command.php';\n",
            b"<?php\nrequire __DIR__ . '/other.php';\n",
            b"<?php\nfile_put_contents('/tmp/pwned', 'x');\n",
        )

        class NoRemote:
            def __getattr__(self, name):
                raise AssertionError("remote activity is forbidden: " + name)

        class NoRemoteDeployer:
            ftps = NoRemote()
            remote_validator = NoRemote()
            validation_context = {"expected_hosts": ["example.xsrv.jp"]}

        for body in variants:
            with self.subTest(body=body):
                wrapper.write_bytes(body)
                wrapper.chmod(0o700)
                workflow = ReleaseWorkflow(
                    NoRemoteDeployer(), "/home/example", "/home/example/private/config.json"
                )
                with self.assertRaises(ReleaseWorkflowError):
                    workflow.provision_fixed_runtime(self.source)

    def test_partial_corrupt_or_wrong_mode_fixed_runtime_fails_without_overwrite(self):
        fixed = "/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php"
        self.deployer.ftps.files[fixed] = b"partial"
        self.deployer.ftps.modes[fixed] = "700"
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual([], self.deployer.ftps.deployments)

        self.deployer = FakeReleaseDeployer()
        self.workflow = ReleaseWorkflow(self.deployer, "/home/example", "/home/example/private/config.json")
        self.workflow.provision_fixed_runtime(self.source)
        self.deployer.ftps.files[fixed] = b"corrupt"
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)
        count = len(self.deployer.ftps.deployments)
        self.deployer.ftps.files[fixed] = (self.source / "bin/stable-mail-entrypoint.php").read_bytes()
        self.deployer.ftps.modes[fixed] = "600"
        with self.assertRaises(RuntimeError):
            self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual(count, len(self.deployer.ftps.deployments))

        wrapper = "/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php"
        self.deployer.ftps.modes[fixed] = "700"
        self.deployer.ftps.modes[wrapper] = "700"
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)
        self.deployer.ftps.modes[wrapper] = "711"
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)

    def test_ssh_preflight_failure_stops_before_any_upload_and_partial_generation_retries(self):
        class Rejecting:
            def inspect_fixed_runtime(self, *_args, **_kwargs):
                raise RuntimeError("unsafe parent or symlink")
        self.deployer.remote_validator = Rejecting()
        with self.assertRaises(RuntimeError):
            self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual([], self.deployer.ftps.deployments)

        class PartialThenExact(FakeRemoteValidator):
            def inspect_fixed_runtime(self, root, entries, *, expected_hosts):
                if ".fixed-staging-" in root and not self.ftps.deployments:
                    return "PARTIAL"
                return super().inspect_fixed_runtime(root, entries, expected_hosts=expected_hosts)
        self.deployer.remote_validator = PartialThenExact(self.deployer.ftps)
        self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual(1, len(self.deployer.ftps.cleanups))
        self.assertTrue(self.deployer.ftps.cleanups[0].startswith("/private/.fixed-staging-"))

    def test_partial_final_with_no_ftps_files_never_uploads_deletes_or_renames(self):
        class PartialFinal:
            def inspect_fixed_runtime(self, *_args, **_kwargs): return "PARTIAL"
        self.deployer.remote_validator = PartialFinal()
        with self.assertRaises(ReleaseWorkflowError):
            self.workflow.provision_fixed_runtime(self.source)
        self.assertEqual([], self.deployer.ftps.deployments)
        self.assertEqual([], self.deployer.ftps.cleanups)
        self.assertEqual({}, self.deployer.ftps.files)


if __name__ == "__main__":
    unittest.main()
