import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import io
import sys
from pathlib import Path
from unittest.mock import patch

from macos.install_app import (
    BUNDLE_IDENTIFIER,
    EXPECTED_BUNDLE_FILES,
    InstallError,
    build_bundle,
    ensure_unprivileged,
    relative_bundle_files,
    validate_bundle,
    validate_installer_python,
    write_config_atomic,
    write_config_with_confirmation,
    install_bundle,
    install_with_config,
    InstallOutcome,
    recover_transactions,
    main,
    verify_installation_completion,
)
from macos.local_config import load_config, LocalConfigError
from macos.runtime import record_python_runtime
from macos.install_transaction import RecoveryError


VALID_CONFIG = {
    "servername": "example.xsrv.jp",
    "command_path": "/home/example/command",
    "ftps_host": "ftp.example.invalid",
    "config_path": "/home/example/config.json",
    "filesystem_home": "/home/example",
    "ssh_alias": "xserver-production",
}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        (self.home / "Library").mkdir(mode=0o700)
        (self.home / "Library" / "Application Support").mkdir(mode=0o700)
        self.config_path = self.home / "Library" / "Application Support" / "XserverMailLineworks" / "config.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_forwarding_aware_sync_release_resources_and_documentation(self):
        repository = Path(__file__).parents[2]
        readme = (repository / "README.md").read_text(encoding="utf-8")
        example = json.loads((repository / "config/config.example.json").read_text(encoding="utf-8"))
        wrapper = repository / "bin/mail-forward-command-701.php"
        self.assertEqual(
            b"<?php\nrequire __DIR__ . '/mail-forward-command.php';\n",
            wrapper.read_bytes(),
        )
        self.assertNotIn("public_html", wrapper.as_posix().casefold())

        for source_name, packaged in (
            ("manager/manage.py", "Contents/Resources/manager/manage.py"),
            ("manager/xserver_api.py", "Contents/Resources/manager/xserver_api.py"),
        ):
            self.assertTrue((repository / source_name).is_file())
            self.assertIn(packaged, EXPECTED_BUNDLE_FILES)
        self.assertIn("notification_base_address", example)
        self.assertEqual("operator@example.invalid", example["notification_base_address"])
        self.assertTrue(example["dedup_path"].startswith("/home/example/private/"))
        self.assertNotIn("/public_html/", example["dedup_path"].lower())
        for documented_behavior in (
            "メニュー13",
            '`field` が `header`',
            '`match_type` が `contain`',
            "転送元を自動検出",
            "dry-run",
            "通知対象を同期する",
            "ロールバック",
        ):
            self.assertIn(documented_behavior, readme)
        for unsupported_claim in (
            "解析できた可視ヘッダー",
            "表示名付きのアドレスも正規化",
        ):
            self.assertNotIn(unsupported_claim, readme)

    def test_installer_rejects_sudo_and_python_312(self):
        with self.assertRaises(InstallError):
            ensure_unprivileged(real_uid=501, effective_uid=0)
        with self.assertRaises(InstallError):
            validate_installer_python((3, 12), "/usr/bin/python3")

    def test_installer_python_uses_shared_runtime_recorder(self):
        executable = self.root / "python"
        executable.write_bytes(b"python")
        expected = record_python_runtime(str(executable), (3, 13))
        self.assertEqual(expected, validate_installer_python((3, 13), str(executable)))

    def test_existing_config_is_not_overwritten_without_exact_confirmation(self):
        self.config_path.parent.mkdir(mode=0o700)
        old = b"old bytes"
        self.config_path.write_bytes(old)
        self.config_path.chmod(0o600)
        with patch("macos.install_app.Path.home", return_value=self.home):
            self.assertFalse(write_config_with_confirmation(self.config_path, VALID_CONFIG, "いいえ", os.getuid()))
        self.assertEqual(old, self.config_path.read_bytes())

    def test_atomic_config_rollback_restores_old_bytes_and_commit_discards_backup(self):
        self.config_path.parent.mkdir(mode=0o700)
        old = b'{"old":true}\n'
        self.config_path.write_bytes(old)
        self.config_path.chmod(0o600)
        with patch("macos.install_app.Path.home", return_value=self.home), patch("macos.local_config.Path.home", return_value=self.home):
            transaction = write_config_atomic(self.config_path, VALID_CONFIG, os.getuid())
            self.assertEqual(VALID_CONFIG, load_config(self.config_path, os.getuid()))
            transaction.rollback()
        self.assertEqual(old, self.config_path.read_bytes())

        with patch("macos.install_app.Path.home", return_value=self.home):
            transaction = write_config_atomic(self.config_path, VALID_CONFIG, os.getuid())
            transaction.commit()
        self.assertEqual({"config.json"}, {p.name for p in self.config_path.parent.iterdir()})

    def test_config_transaction_is_single_use_and_canonical(self):
        with patch("macos.install_app.Path.home", return_value=self.home):
            transaction = write_config_atomic(self.config_path, VALID_CONFIG, os.getuid())
            transaction.commit()
            with self.assertRaises(InstallError):
                transaction.rollback()
        expected = (json.dumps(VALID_CONFIG, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self.assertEqual(expected, self.config_path.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(self.config_path.stat().st_mode))

    def test_common_transaction_marker_is_durable_before_config_rename(self):
        self.config_path.parent.mkdir(mode=0o700)
        self.config_path.write_bytes(b'{"old":true}\n')
        self.config_path.chmod(0o600)
        txn_id = "a" * 32
        real_rename = os.rename

        def inspect_rename(src, dst, **kwargs):
            if src == "config.json":
                marker = self.config_path.parent / f".transaction-{txn_id}.json"
                self.assertTrue(marker.exists())
                value = json.loads(marker.read_text())
                self.assertEqual((txn_id, "TXN_PREPARED"), (value["txn_id"], value["phase"]))
                self.assertEqual(0o600, stat.S_IMODE(marker.stat().st_mode))
            return real_rename(src, dst, **kwargs)

        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.install_app.os.rename", side_effect=inspect_rename):
            transaction = write_config_atomic(self.config_path, VALID_CONFIG, os.getuid(), txn_id=txn_id)
            self.assertEqual(txn_id, transaction.txn_id)
            transaction.rollback()
        self.assertFalse((self.config_path.parent / f".transaction-{txn_id}.json").exists())

    def test_common_app_journal_callback_runs_after_temp_fsync_before_config_mutation(self):
        self.config_path.parent.mkdir(mode=0o700)
        old = b'{"old":true}\n'
        self.config_path.write_bytes(old); self.config_path.chmod(0o600)
        txn_id = "9" * 32
        observed = []
        def record(marker):
            self.assertEqual(old, self.config_path.read_bytes())
            self.assertTrue((self.config_path.parent / marker["temp"]).is_file())
            self.assertTrue((self.config_path.parent / f".transaction-{txn_id}.json").is_file())
            observed.append(marker)
        with patch("macos.install_app.Path.home", return_value=self.home):
            transaction = write_config_atomic(self.config_path, VALID_CONFIG, os.getuid(),
                                              txn_id=txn_id, before_publish=record)
            transaction.rollback()
        self.assertEqual(1, len(observed))

    def test_fresh_recovery_rolls_shared_config_back_by_hash(self):
        from macos.install_app import _recover_config_marker
        self.config_path.parent.mkdir(mode=0o700)
        old = b'{"old":true}\n'
        self.config_path.write_bytes(old)
        self.config_path.chmod(0o600)
        txn_id = "c" * 32
        with patch("macos.install_app.Path.home", return_value=self.home):
            transaction = write_config_atomic(self.config_path, VALID_CONFIG, os.getuid(), txn_id=txn_id)
            # Simulate process death: the live object vanishes without commit/rollback.
            os.close(transaction._app_fd)
            transaction._app_fd = -1
            _recover_config_marker(txn_id, os.getuid(), app_state="old")
        self.assertEqual(old, self.config_path.read_bytes())
        self.assertEqual({"config.json"}, {p.name for p in self.config_path.parent.iterdir()})

    def test_expected_config_intent_rejects_missing_configuration_directory(self):
        from macos.install_app import _recover_config_marker
        marker = {"schema": 1, "txn_id": "7" * 32, "phase": "TXN_PREPARED",
                  "temp": ".config-" + "7" * 32, "backup": None,
                  "trash": ".trash-" + "7" * 32, "old_config": False,
                  "old_sha256": None, "new_sha256": "0" * 64}
        with patch("macos.install_app.Path.home", return_value=self.home), \
                self.assertRaisesRegex(InstallError, "隔離"):
            _recover_config_marker("7" * 32, os.getuid(), app_state="new", expected_marker=marker)

    def test_second_recovery_accepts_crash_after_backup_restore(self):
        from macos.install_app import _recover_config_marker
        self.config_path.parent.mkdir(mode=0o700)
        old = b'{"old":true}\n'
        self.config_path.write_bytes(old)
        self.config_path.chmod(0o600)
        txn_id = "d" * 32
        with patch("macos.install_app.Path.home", return_value=self.home):
            transaction = write_config_atomic(self.config_path, VALID_CONFIG, os.getuid(), txn_id=txn_id)
            os.close(transaction._app_fd)
            transaction._app_fd = -1
            self.config_path.unlink()
            os.rename(self.config_path.parent / transaction._backup_name, self.config_path)
            _recover_config_marker(txn_id, os.getuid(), app_state="old")
        self.assertEqual(old, self.config_path.read_bytes())

    def test_production_recovery_executes_all_fifteen_matrix_cells(self):
        from macos.install_app import _recover_config_marker
        import hashlib
        old, new = b"old\n", b"new\n"
        layouts = {
            "a": {"config.json": old, "temp": new},
            "b": {"backup": old, "temp": new},
            "c": {"config.json": new, "backup": old},
            "d": {"temp": new},
            "e": {"config.json": new},
        }
        for cell in "abcde":
            for app_state in ("absent", "old", "new"):
                with self.subTest(cell=cell, app_state=app_state):
                    if self.config_path.parent.exists():
                        shutil.rmtree(self.config_path.parent)
                    self.config_path.parent.mkdir(mode=0o700)
                    txn_id = (cell + {"absent": "0", "old": "1", "new": "2"}[app_state]) * 16
                    temp = f".config-{txn_id}"
                    backup = f".backup-{'a' * 32}" if cell in "abc" else None
                    names = {"temp": temp, "backup": backup}
                    for key, body in layouts[cell].items():
                        name = names.get(key, key)
                        (self.config_path.parent / name).write_bytes(body)
                        (self.config_path.parent / name).chmod(0o600)
                    marker = {"schema": 1, "txn_id": txn_id, "phase": "TXN_PREPARED",
                              "temp": temp, "backup": backup, "trash": f".trash-{txn_id}",
                              "old_config": cell in "abc",
                              "old_sha256": hashlib.sha256(old).hexdigest() if cell in "abc" else None,
                              "new_sha256": hashlib.sha256(new).hexdigest()}
                    marker_path = self.config_path.parent / f".transaction-{txn_id}.json"
                    marker_path.write_text(json.dumps(marker, sort_keys=True)); marker_path.chmod(0o600)
                    with patch("macos.install_app.Path.home", return_value=self.home):
                        _recover_config_marker(txn_id, os.getuid(), app_state=app_state)
                    expected = new if app_state == "new" else (old if cell in "abc" else None)
                    self.assertEqual(expected, self.config_path.read_bytes() if self.config_path.exists() else None)
                    self.assertEqual({"config.json"} if expected is not None else set(),
                                     {p.name for p in self.config_path.parent.iterdir()})

    def test_each_production_recovery_action_survives_a_second_process_crash(self):
        import hashlib, subprocess
        old, new = b"old\n", b"new\n"
        cases = (("a", "new", "OLD_TO_BACKUP"), ("d", "new", "PUBLISH_TEMP"),
                 ("c", "new", "DROP_BACKUP"), ("b", "old", "RESTORE_BACKUP"),
                 ("a", "old", "DROP_TEMP"), ("e", "old", "NEW_TO_TRASH"),
                 ("e", "old", "DROP_TRASH"))
        program = """from pathlib import Path
from unittest.mock import patch
import os,sys
from macos.install_app import _recover_config_marker
home=Path(sys.argv[1]); txn,app,target=sys.argv[2:5]
def fault(action):
    if action == target: raise SystemExit(77)
with patch('macos.install_app.Path.home', return_value=home):
    _recover_config_marker(txn, os.getuid(), app_state=app, _fault=fault if target != '-' else None)
"""
        root = str(Path(__file__).parents[2])
        for cell, app_state, target in cases:
            with self.subTest(cell=cell, app_state=app_state, target=target):
                if self.config_path.parent.exists(): shutil.rmtree(self.config_path.parent)
                self.config_path.parent.mkdir(mode=0o700)
                txn_id = (cell + "f") * 16
                temp, backup = f".config-{txn_id}", (f".backup-{'b' * 32}" if cell in "abc" else None)
                layouts = {"a": {"config.json": old, temp: new}, "b": {backup: old, temp: new},
                           "c": {"config.json": new, backup: old}, "d": {temp: new}, "e": {"config.json": new}}
                for name, body in layouts[cell].items():
                    (self.config_path.parent / name).write_bytes(body); (self.config_path.parent / name).chmod(0o600)
                marker = {"schema": 1, "txn_id": txn_id, "phase": "TXN_PREPARED", "temp": temp,
                          "backup": backup, "trash": f".trash-{txn_id}", "old_config": cell in "abc",
                          "old_sha256": hashlib.sha256(old).hexdigest() if cell in "abc" else None,
                          "new_sha256": hashlib.sha256(new).hexdigest()}
                marker_path = self.config_path.parent / f".transaction-{txn_id}.json"
                marker_path.write_text(json.dumps(marker)); marker_path.chmod(0o600)
                first = subprocess.run([sys.executable, "-c", program, str(self.home), txn_id, app_state, target], cwd=root)
                self.assertEqual(77, first.returncode)
                second = subprocess.run([sys.executable, "-c", program, str(self.home), txn_id, app_state, "-"], cwd=root)
                self.assertEqual(0, second.returncode)
                third = subprocess.run([sys.executable, "-c", program, str(self.home), txn_id, app_state, "-"], cwd=root)
                self.assertEqual(0, third.returncode)

    def test_recovery_after_mutation_is_resumed_by_two_fresh_processes(self):
        import hashlib, subprocess
        old, new = b"old\n", b"new\n"
        txn_id = "a1" * 16
        self.config_path.parent.mkdir(mode=0o700)
        temp, backup = f".config-{txn_id}", f".backup-{'c' * 32}"
        for name, body in (("config.json", old), (temp, new)):
            (self.config_path.parent / name).write_bytes(body); (self.config_path.parent / name).chmod(0o600)
        marker = {"schema": 1, "txn_id": txn_id, "phase": "TXN_PREPARED", "temp": temp,
                  "backup": backup, "trash": f".trash-{txn_id}", "old_config": True,
                  "old_sha256": hashlib.sha256(old).hexdigest(), "new_sha256": hashlib.sha256(new).hexdigest()}
        marker_path = self.config_path.parent / f".transaction-{txn_id}.json"
        marker_path.write_text(json.dumps(marker)); marker_path.chmod(0o600)
        program = """from pathlib import Path
from unittest.mock import patch
import os,sys
from macos.install_app import _recover_config_marker
home=Path(sys.argv[1]); txn=sys.argv[2]; crash=sys.argv[3]=='crash'
def fault(action): raise SystemExit(77)
with patch('macos.install_app.Path.home', return_value=home):
    _recover_config_marker(txn, os.getuid(), app_state='new', _fault=fault if crash else None)
"""
        root = str(Path(__file__).parents[2])
        first = subprocess.run([sys.executable, "-c", program, str(self.home), txn_id, "crash"], cwd=root)
        self.assertEqual(77, first.returncode)
        second = subprocess.run([sys.executable, "-c", program, str(self.home), txn_id, "resume"], cwd=root)
        self.assertEqual(0, second.returncode)
        self.assertEqual(new, self.config_path.read_bytes())
        self.assertEqual({"config.json"}, {p.name for p in self.config_path.parent.iterdir()})

    def test_rejects_untrusted_source_and_existing_build_roots(self):
        source = self._source_fixture()
        source.chmod(0o777)
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        with self.assertRaises(InstallError):
            build_bundle(source, self.root / "build", record)
        source.chmod(0o700)
        build = self.root / "existing"
        build.mkdir(mode=0o700)
        with self.assertRaises(InstallError):
            build_bundle(source, build, record)

    def test_bundle_contains_exact_runtime_package_and_generated_files(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        helper_relative = "Contents/Resources/manager/private_config_ssh.py"
        self.assertIn(helper_relative, EXPECTED_BUNDLE_FILES)
        bundle = build_bundle(source, self.root / "build", record)
        self.assertEqual(EXPECTED_BUNDLE_FILES, relative_bundle_files(bundle))
        validate_bundle(bundle, source, os.getuid())
        bundled_manage = (bundle / "Contents/Resources/manager/manage.py").read_text(encoding="utf-8")
        self.assertIn('"14": self.show_webhook_url', bundled_manage)
        self.assertIn('"15": self.change_webhook_url', bundled_manage)
        bundled_helper = bundle / helper_relative
        self.assertEqual((source / "manager/private_config_ssh.py").read_bytes(), bundled_helper.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(bundled_helper.stat().st_mode))
        fixed = bundle / "Contents/Resources/fixed-runtime"
        self.assertEqual((source / "bin/manage-private-config.php").read_bytes(),
                         (fixed / "manage-private-config.php").read_bytes())
        self.assertEqual((source / "fixed-runtime/legacy-manifest.json").read_bytes(),
                         (fixed / "legacy-manifest.json").read_bytes())
        self.assertEqual({0o600}, {
            stat.S_IMODE((fixed / name).stat().st_mode)
            for name in ("manage-private-config.php", "legacy-manifest.json")
        })
        plist = shutil.which("plutil")
        self.assertIsNotNone(plist)

    def test_bundle_contains_fixed_generation_manifest(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        relative = "Contents/Resources/fixed-runtime/generation-b9fd468-manifest.json"
        self.assertIn(relative, EXPECTED_BUNDLE_FILES)
        bundle = build_bundle(source, self.root / "generation-build", record)
        bundled = bundle / relative
        expected = source / "fixed-runtime/generation-b9fd468-manifest.json"
        self.assertEqual(expected.read_bytes(), bundled.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(bundled.stat().st_mode))
        validate_bundle(bundle, source, os.getuid())

    def test_bundle_rejects_generation_manifest_omission_tamper_symlink_mode_and_extra(self):
        source = self._source_fixture()
        for variant in ("omission", "tamper", "symlink", "mode", "extra"):
            with self.subTest(variant=variant):
                executable = self.root / ("python-" + variant)
                executable.write_bytes(b"python")
                record = record_python_runtime(str(executable), (3, 13))
                bundle = build_bundle(source, self.root / ("generation-" + variant), record)
                target = (bundle / "Contents/Resources/fixed-runtime"
                          / "generation-b9fd468-manifest.json")
                if variant == "omission":
                    target.unlink()
                elif variant == "tamper":
                    target.write_bytes(target.read_bytes() + b" ")
                    target.chmod(0o600)
                elif variant == "symlink":
                    outside = self.root / "outside-generation.json"
                    outside.write_bytes(target.read_bytes())
                    target.unlink(); target.symlink_to(outside)
                elif variant == "mode":
                    target.chmod(0o700)
                else:
                    extra = target.parent / "unknown-generation.json"
                    extra.write_bytes(b"{}\n"); extra.chmod(0o600)
                with self.assertRaises(InstallError):
                    validate_bundle(bundle, source, os.getuid())

    def test_bundle_is_signed_with_fixed_identifier_after_final_plist_update(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "signed-build", record)
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, verified.returncode, verified.stderr)
        details = subprocess.run(
            ["/usr/bin/codesign", "-dvv", str(bundle)],
            capture_output=True, text=True, check=False,
        )
        self.assertIn("Identifier=jp.example.xserver-mail-lineworks-manager", details.stderr)
        self.assertIn("Info.plist entries=", details.stderr)

    def test_bundled_manager_reaches_environment_boundary_from_minimal_environment(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "manager-start-build", record)
        completed = subprocess.run(
            [sys.executable, "-B", str(bundle / "Contents/Resources/manager/manage.py")],
            cwd=self.root, env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertIn("環境設定が不足しています", completed.stderr)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)

    def test_validate_bundle_rejects_unknown_symlink_and_secret_without_disclosure(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "build", record)
        unknown = bundle / "Contents" / "Resources" / "unknown"
        unknown.write_text("x")
        with self.assertRaises(InstallError):
            validate_bundle(bundle, source, os.getuid())
        unknown.unlink()
        launcher = bundle / "Contents" / "Resources" / "launcher.sh"
        token = b"https://webhook.worksmobile.com/" + b"message/this-is-a-real-secret-token-value"
        launcher.write_bytes(token)
        with self.assertRaises(InstallError) as caught:
            validate_bundle(bundle, source, os.getuid())
        self.assertNotIn("real-secret", str(caught.exception))

    def test_build_compiles_trusted_bytes_over_stdin_without_a_source_path(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        real_run = __import__("subprocess").run

        def inspect_compile(argv, **kwargs):
            if argv[0] == "/usr/bin/osacompile":
                self.assertEqual("-", argv[-1])
                self.assertEqual((source / "macos/AppLauncher.applescript").read_bytes(), kwargs["input"])
            return real_run(argv, **kwargs)

        with patch("macos.install_app.subprocess.run", side_effect=inspect_compile):
            build_bundle(source, self.root / "build", record)

    def test_validate_bundle_rejects_special_file_and_runtime_corruption(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "build", record)
        launcher = bundle / "Contents/Resources/launcher.sh"
        launcher.unlink()
        os.mkfifo(launcher, 0o600)
        with self.assertRaises(InstallError):
            validate_bundle(bundle, source, os.getuid())
        launcher.unlink()
        shutil.copyfile(source / "macos/launcher.sh", launcher)
        launcher.chmod(0o700)
        metadata = bundle / "Contents/Resources/python-runtime.json"
        metadata.write_text(json.dumps({"path": record.path, "device": True, "inode": record.inode, "major": 3, "minor": 13}))
        metadata.chmod(0o600)
        with self.assertRaises(InstallError):
            validate_bundle(bundle, source, os.getuid())

    def test_validate_bundle_rejects_runtime_identity_metadata_change(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "build", record)
        metadata = bundle / "Contents/Resources/python-runtime.json"
        value = json.loads(metadata.read_text())
        value["inode"] += 1
        metadata.write_text(json.dumps(value))
        metadata.chmod(0o600)
        with self.assertRaises(InstallError):
            validate_bundle(bundle, source, os.getuid())

    def test_validate_bundle_rejects_untrusted_source_root(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "build", record)
        source.chmod(0o777)
        with self.assertRaises(InstallError):
            validate_bundle(bundle, source, os.getuid())

    def test_secret_scanner_covers_api_keys_and_secret_names(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        for index, token in enumerate((b"XSERVER_API_KEY=0123456789abcdef0123456789abcdef", b"xs_NonHex-API_Key.Value", b"private-secret-config.json")):
            bundle = build_bundle(source, self.root / f"build-{index}", record)
            target = bundle / "Contents/Resources/launcher.sh"
            target.write_bytes(token)
            target.chmod(0o700)
            with self.assertRaises(InstallError):
                validate_bundle(bundle, source, os.getuid())

    def test_secret_scanner_accepts_known_secret_denylist_without_disclosure(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "build", record)
        secret = b"tenant-specific-value-8472"
        target = bundle / "Contents/Resources/launcher.sh"
        target.write_bytes(secret)
        target.chmod(0o700)
        with self.assertRaises(InstallError) as caught:
            validate_bundle(bundle, source, os.getuid(), forbidden_tokens=(secret,))
        self.assertNotIn(secret.decode(), str(caught.exception))

    def test_bundle_symlink_rejection_does_not_chmod_outside_target(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        bundle = build_bundle(source, self.root / "build", record)
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        outside.chmod(0o644)
        launcher = bundle / "Contents/Resources/launcher.sh"
        launcher.unlink()
        launcher.symlink_to(outside)
        with self.assertRaises(InstallError):
            validate_bundle(bundle, source, os.getuid())
        self.assertEqual(0o644, stat.S_IMODE(outside.stat().st_mode))

    def test_config_fsync_failure_restores_old_bytes(self):
        self.config_path.parent.mkdir(mode=0o700)
        old = b'{"old":true}\n'
        self.config_path.write_bytes(old)
        self.config_path.chmod(0o600)
        real_fsync = os.fsync
        calls = 0

        def fail_once(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fsync failure")
            return real_fsync(fd)

        with patch("macos.install_app.Path.home", return_value=self.home), patch("macos.install_app.os.fsync", side_effect=fail_once):
            with self.assertRaises(OSError):
                write_config_atomic(self.config_path, VALID_CONFIG, os.getuid())
        self.assertEqual(old, self.config_path.read_bytes())

    def test_build_failure_removes_partial_bundle_output(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))

        def partial_failure(argv, **_kwargs):
            bundle = Path(argv[argv.index("-o") + 1])
            (bundle / "Contents").mkdir(parents=True)
            (bundle / "Contents/partial").write_bytes(b"partial")
            raise OSError("compiler failure")

        with patch("macos.install_app.subprocess.run", side_effect=partial_failure):
            with self.assertRaises(InstallError):
                build_bundle(source, self.root / "build", record)
        self.assertFalse((self.root / "build").exists())

    def test_build_rejects_compiler_symlink_before_chmod_without_touching_target(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        outside = self.root / "outside-executable"
        outside.write_bytes(b"outside")
        outside.chmod(0o644)
        real_run = __import__("subprocess").run

        def replace_generated_entry(argv, **kwargs):
            result = real_run(argv, **kwargs)
            if argv[0] == "/usr/bin/osacompile":
                generated = Path(argv[argv.index("-o") + 1]) / "Contents/MacOS/applet"
                generated.unlink()
                generated.symlink_to(outside)
            return result

        with patch("macos.install_app.subprocess.run", side_effect=replace_generated_entry):
            with self.assertRaises(InstallError):
                build_bundle(source, self.root / "build", record)
        self.assertEqual(0o644, stat.S_IMODE(outside.stat().st_mode))

    def test_build_rejects_compiler_info_plist_symlink_before_mutation(self):
        source = self._source_fixture()
        executable = self.root / "python"
        executable.write_bytes(b"python")
        record = record_python_runtime(str(executable), (3, 13))
        outside = self.root / "outside-info.plist"
        original = __import__("plistlib").dumps({"Outside": "must remain unchanged"})
        outside.write_bytes(original)
        outside.chmod(0o644)
        real_run = __import__("subprocess").run

        def replace_generated_plist(argv, **kwargs):
            result = real_run(argv, **kwargs)
            if argv[0] == "/usr/bin/osacompile":
                generated = Path(argv[argv.index("-o") + 1]) / "Contents/Info.plist"
                generated.unlink()
                generated.symlink_to(outside)
            return result

        with patch("macos.install_app.subprocess.run", side_effect=replace_generated_plist):
            with self.assertRaises(InstallError):
                build_bundle(source, self.root / "build", record)
        self.assertEqual(original, outside.read_bytes())
        self.assertEqual(0o644, stat.S_IMODE(outside.stat().st_mode))

    def test_main_runs_production_install_with_config(self):
        source = self._source_fixture()
        build = self.root / "staging"
        build.mkdir(mode=0o700)
        answers = iter(VALID_CONFIG.values())
        output = io.StringIO()
        with patch("macos.install_app.ensure_unprivileged"), \
                patch("macos.install_app.input", side_effect=lambda _prompt: next(answers)), \
                patch("macos.install_app.tempfile.mkdtemp", return_value=str(build)), \
                patch("macos.install_app.install_with_config", return_value=InstallOutcome.NEW_INSTALLED) as installer, \
                patch("macos.install_app.Path.home", return_value=self.home), \
                patch("sys.stdout", output):
            self.assertEqual(0, main(["--source-root", str(source)]))
        installer.assert_called_once_with(source, build, VALID_CONFIG, os.getuid())
        self.assertIn("インストール完了", output.getvalue())

    def test_main_rejects_real_effective_uid_mismatch_before_prompt(self):
        with patch("macos.install_app.os.getuid", return_value=501), \
                patch("macos.install_app.os.geteuid", return_value=0), \
                patch("macos.install_app.input") as prompt:
            self.assertEqual(1, main(["--source-root", str(self.root)]))
        prompt.assert_not_called()

    def test_main_verify_latest_uses_completion_receipt_without_install_prompts(self):
        with patch("macos.install_app.ensure_unprivileged"), \
                patch("macos.install_app.verify_installation_completion",
                      return_value={"result_classification": "INSTALLATION_COMPLETED"}) as verify, \
                patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertEqual(0, main(["--verify-completion-receipt", "--latest"]))
        verify.assert_called_once_with(os.getuid())

    def test_install_bundle_rejects_every_destination_except_user_applications(self):
        with patch("macos.install_app.Path.home", return_value=self.home):
            with self.assertRaisesRegex(InstallError, "destination"):
                install_bundle(self.root / "bundle.app", self.home / "Desktop/bundle.app", os.getuid())

    def test_existing_destination_requires_fixed_identifier_and_exact_manifest(self):
        from macos.install_app import _validate_existing_destination
        destination = self.root / "existing.app"
        destination.mkdir(mode=0o700)
        with patch("macos.install_app._check_bundle_identifier") as identifier, \
                patch("macos.install_app.relative_bundle_files", return_value=set(EXPECTED_BUNDLE_FILES)), \
                patch("macos.install_app.create_bundle_manifest", return_value=unittest.mock.sentinel.manifest) as manifest:
            self.assertIs(unittest.mock.sentinel.manifest,
                          _validate_existing_destination(destination, os.getuid()))
        identifier.assert_called_once_with(destination)
        self.assertEqual(set(EXPECTED_BUNDLE_FILES), manifest.call_args.kwargs["allowed_files"])

    def test_known_legacy_exact_bundle_can_be_updated_to_current_bundle(self):
        source = self._source_fixture()
        record = record_python_runtime(str(Path(sys.executable).resolve()), tuple(sys.version_info[:2]))
        bundle = build_bundle(source, self.root / "legacy-update-build", record)
        applications = self.home / "Applications"
        applications.mkdir(mode=0o700)
        destination = applications / "Xserverメール通知管理.app"
        shutil.copytree(bundle, destination)
        (destination / "Contents/Resources/manager/private_config_ssh.py").unlink()
        shutil.rmtree(destination / "Contents/Resources/fixed-runtime")

        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.install_app.sys.executable", str(Path(sys.executable).resolve())):
            outcome = install_with_config(
                source, self.root / "legacy-update-current-build", VALID_CONFIG, os.getuid()
            )

        self.assertEqual(InstallOutcome.UPDATED, outcome)
        self.assertEqual(EXPECTED_BUNDLE_FILES, relative_bundle_files(destination))
        validate_bundle(destination, source, os.getuid())

    def test_existing_destination_legacy_exception_rejects_every_other_layout(self):
        import plistlib
        from macos.install_app import _validate_existing_destination

        source = self._source_fixture()
        record = record_python_runtime(str(Path(sys.executable).resolve()), tuple(sys.version_info[:2]))
        bundle = build_bundle(source, self.root / "legacy-guard-build", record)
        variants = ("extra", "other-missing", "identifier-mismatch")
        for variant in variants:
            with self.subTest(variant=variant):
                destination = self.root / (variant + ".app")
                shutil.copytree(bundle, destination)
                if variant == "extra":
                    extra = destination / "Contents/Resources/manager/unexpected.py"
                    extra.write_text("unexpected\n", encoding="utf-8")
                    extra.chmod(0o600)
                elif variant == "other-missing":
                    (destination / "Contents/Resources/manager/keychain.py").unlink()
                else:
                    plist_path = destination / "Contents/Info.plist"
                    plist = plistlib.loads(plist_path.read_bytes())
                    plist["CFBundleIdentifier"] = "jp.example.wrong"
                    plist_path.write_bytes(plistlib.dumps(plist))
                with self.assertRaises(InstallError):
                    _validate_existing_destination(destination, os.getuid())

    def test_install_with_config_rolls_back_when_app_publication_fails(self):
        transaction = unittest.mock.Mock()
        lease = unittest.mock.Mock()
        lease.txn_id = "8" * 32
        bundle = self.root / "built/Xserverメール通知管理.app"
        failure = InstallError("publication failed")
        lease.publish.side_effect = failure
        with patch("macos.install_app.validate_installer_python", return_value=unittest.mock.sentinel.runtime), \
                patch("macos.install_app.build_bundle", return_value=bundle), \
                patch("macos.install_app.recover_pending_transactions"), \
                patch("macos.install_app._prepare_common_transaction", return_value=lease), \
                patch("macos.install_app.write_config_atomic", return_value=transaction), \
                patch("macos.install_app.Path.home", return_value=self.home):
            with self.assertRaisesRegex(InstallError, "publication failed"):
                install_with_config(self.root / "source", self.root / "build", VALID_CONFIG, os.getuid())
        transaction.rollback.assert_called_once_with()
        transaction.commit.assert_not_called()
        lease.abort.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_install_with_config_recovers_pending_work_before_build_or_config(self):
        calls = []
        with patch("macos.install_app.recover_pending_transactions", side_effect=lambda uid: calls.append(("recover", uid))), \
                patch("macos.install_app.validate_installer_python", return_value=unittest.mock.sentinel.runtime), \
                patch("macos.install_app.build_bundle", side_effect=lambda *args: calls.append(("build",)) or self.root / "app"), \
                patch("macos.install_app._prepare_common_transaction", side_effect=lambda *args: calls.append(("prepare",)) or (_ for _ in ()).throw(InstallError("stop"))), \
                patch("macos.install_app.Path.home", return_value=self.home):
            with self.assertRaises(InstallError):
                install_with_config(self.root, self.root / "build", VALID_CONFIG, os.getuid())
        self.assertEqual("recover", calls[0][0])

    def test_install_with_config_commits_only_after_verified_publication(self):
        transaction = unittest.mock.Mock()
        bundle = self.root / "built/Xserverメール通知管理.app"
        lease = unittest.mock.Mock()
        lease.txn_id = "8" * 32
        lease.publish.return_value = InstallOutcome.UPDATED
        with patch("macos.install_app.recover_pending_transactions"), \
                patch("macos.install_app.validate_installer_python", return_value=unittest.mock.sentinel.runtime), \
                patch("macos.install_app.build_bundle", return_value=bundle), \
                patch("macos.install_app._prepare_common_transaction", return_value=lease) as prepare, \
                patch("macos.install_app.write_config_atomic", return_value=transaction) as config_writer, \
                patch("macos.install_app.Path.home", return_value=self.home):
            result = install_with_config(self.root / "source", self.root / "build", VALID_CONFIG, os.getuid())
        self.assertEqual(InstallOutcome.UPDATED, result)
        transaction.commit.assert_called_once_with()
        transaction.rollback.assert_not_called()
        config_txn = config_writer.call_args.kwargs["txn_id"]
        self.assertEqual(config_txn, lease.txn_id)
        self.assertRegex(config_txn, r"^[0-9a-f]{32}$")
        prepare.assert_called_once()
        lease.publish.assert_called_once_with()
        lease.mark_committing.assert_called_once_with()
        lease.complete.assert_called_once_with()
        lease.close.assert_called_once_with()

    def test_real_install_call_graph_durably_publishes_config_and_app(self):
        source = self._source_fixture()
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.install_app.sys.executable", str(Path(sys.executable).resolve())):
            outcome = install_with_config(source, self.root / "real-build", VALID_CONFIG, os.getuid())
        self.assertEqual(InstallOutcome.NEW_INSTALLED, outcome)
        self.assertTrue((self.home / "Applications/Xserverメール通知管理.app").is_dir())
        with patch("macos.local_config.Path.home", return_value=self.home):
            self.assertEqual(VALID_CONFIG, load_config(self.config_path, os.getuid()))
        self.assertEqual({"lock", "completed"}, {p.name for p in (self.home / "Applications/.xserver-mail-lineworks-installer").iterdir()})
        receipts = list((self.home / "Applications/.xserver-mail-lineworks-installer/completed").glob("*.json"))
        self.assertEqual(1, len(receipts))
        self.assertNotIn(str(self.home), receipts[0].read_text())
        receipt_payload = json.loads(receipts[0].read_text())
        installed_manifest = __import__("macos.install_transaction", fromlist=["create_bundle_manifest"]).create_bundle_manifest(
            self.home / "Applications/Xserverメール通知管理.app", os.getuid(),
            allowed_files=set(EXPECTED_BUNDLE_FILES),
            allowed_dirs={".", "Contents", "Contents/_CodeSignature", "Contents/MacOS",
                          "Contents/Resources", "Contents/Resources/Scripts",
                          "Contents/Resources/manager", "Contents/Resources/fixed-runtime"})
        self.assertEqual(installed_manifest.tree_sha256, receipt_payload["app_tree_sha256"])
        self.assertEqual(__import__("hashlib").sha256(self.config_path.read_bytes()).hexdigest(),
                         receipt_payload["config_sha256"])
        with patch("macos.install_app.Path.home", return_value=self.home):
            verified = verify_installation_completion(os.getuid())
        self.assertEqual("INSTALLATION_COMPLETED", verified["result_classification"])
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.local_config.Path.home", return_value=self.home), \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(0, main(["--verify-completion-receipt", "--latest"]))

        original_config = self.config_path.read_bytes()
        changed_config = dict(VALID_CONFIG, ssh_alias="xserver-changed")
        self.config_path.write_bytes((json.dumps(changed_config, sort_keys=True, separators=(",", ":")) + "\n").encode())
        self.config_path.chmod(0o600)
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.local_config.Path.home", return_value=self.home), \
                self.assertRaises(RecoveryError):
            verify_installation_completion(os.getuid())
        self.config_path.write_bytes(b'{}')
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.local_config.Path.home", return_value=self.home), \
                self.assertRaises(LocalConfigError):
            verify_installation_completion(os.getuid())
        self.config_path.write_bytes(original_config); self.config_path.chmod(0o600)

        resource = self.home / "Applications/Xserverメール通知管理.app/Contents/Resources/python-path"
        original_mode = stat.S_IMODE(resource.stat().st_mode)
        resource.chmod(0o644 if original_mode != 0o644 else 0o600)
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.local_config.Path.home", return_value=self.home), \
                self.assertRaises((InstallError, RecoveryError)):
            verify_installation_completion(os.getuid())
        resource.chmod(original_mode)

        support = self.home / "Library/Application Support"
        real_support = self.home / "Library/Application Support.real"
        support.rename(real_support); support.symlink_to(real_support, target_is_directory=True)
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.local_config.Path.home", return_value=self.home), \
                self.assertRaises(LocalConfigError):
            verify_installation_completion(os.getuid())
        support.unlink(); real_support.rename(support)

        installer_root = self.home / "Applications/.xserver-mail-lineworks-installer"
        real_verify = __import__("macos.install_app", fromlist=["verify_latest_completion_receipt"]).verify_latest_completion_receipt
        injected = installer_root / ("transaction." + "d" * 32)
        def inject_pending(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            injected.mkdir(mode=0o700)
            return result
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.local_config.Path.home", return_value=self.home), \
                patch("macos.install_app.verify_latest_completion_receipt", side_effect=inject_pending), \
                self.assertRaises(InstallError):
            verify_installation_completion(os.getuid())
        injected.rmdir()

        import fcntl
        lock_fd = os.open(installer_root / "lock", os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("macos.install_app.Path.home", return_value=self.home), \
                    patch("macos.local_config.Path.home", return_value=self.home), \
                    self.assertRaisesRegex(InstallError, "running"):
                verify_installation_completion(os.getuid())
        finally:
            os.close(lock_fd)

    def test_transaction_id_collision_regenerates_a_new_128_bit_id(self):
        from macos.install_app import _prepare_common_transaction
        source = self._source_fixture()
        executable = Path(sys.executable).resolve()
        record = record_python_runtime(str(executable), tuple(sys.version_info[:2]))
        bundle = build_bundle(source, self.root / "collision-build", record)
        real_mkdir = os.mkdir
        collisions = 0
        def collide(path, *args, **kwargs):
            nonlocal collisions
            if str(path).startswith("transaction.") and collisions < 2:
                collisions += 1
                raise FileExistsError(path)
            return real_mkdir(path, *args, **kwargs)
        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.install_app.os.mkdir", side_effect=collide), \
                patch("macos.install_app.secrets.token_hex", side_effect=["2" * 32, "3" * 32]):
            lease = _prepare_common_transaction(bundle, os.getuid(), "1" * 32)
            try:
                self.assertEqual("3" * 32, lease.txn_id)
                lease.abort()
            finally:
                lease.close()

    def test_prejournal_destination_rejection_removes_only_known_staging_and_allows_retry(self):
        from macos.install_app import _prepare_common_transaction
        source = self._source_fixture()
        record = record_python_runtime(str(Path(sys.executable).resolve()), tuple(sys.version_info[:2]))
        bundle = build_bundle(source, self.root / "prejournal-build", record)
        applications = self.home / "Applications"
        applications.mkdir(mode=0o700)
        destination = applications / "Xserverメール通知管理.app"
        destination.write_text("unsafe", encoding="utf-8")

        with patch("macos.install_app.Path.home", return_value=self.home), self.assertRaises(InstallError):
            _prepare_common_transaction(bundle, os.getuid(), "4" * 32)

        installer = applications / ".xserver-mail-lineworks-installer"
        self.assertEqual({"lock"}, {item.name for item in installer.iterdir()})
        destination.unlink()
        with patch("macos.install_app.Path.home", return_value=self.home):
            lease = _prepare_common_transaction(bundle, os.getuid(), "5" * 32)
            try:
                lease.abort()
            finally:
                lease.close()

    def test_prejournal_cleanup_refuses_unknown_staged_tree_replacement(self):
        from macos.install_app import _prepare_common_transaction
        source = self._source_fixture()
        record = record_python_runtime(str(Path(sys.executable).resolve()), tuple(sys.version_info[:2]))
        bundle = build_bundle(source, self.root / "replacement-build", record)
        applications = self.home / "Applications"
        applications.mkdir(mode=0o700)
        destination = applications / "Xserverメール通知管理.app"
        destination.write_text("unsafe", encoding="utf-8")
        outside = self.root / "outside"
        outside.write_text("keep", encoding="utf-8")

        def replace_then_reject(*args):
            staged = next((applications / ".xserver-mail-lineworks-installer").glob("transaction.*/staged.app"))
            shutil.rmtree(staged)
            staged.symlink_to(outside)
            raise InstallError("reject")

        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.install_app._validate_existing_destination", side_effect=replace_then_reject), \
                self.assertRaises(InstallError):
            _prepare_common_transaction(bundle, os.getuid(), "6" * 32)

        self.assertEqual("keep", outside.read_text(encoding="utf-8"))
        transaction = applications / ".xserver-mail-lineworks-installer" / ("transaction." + "6" * 32)
        self.assertTrue((transaction / "staged.app").is_symlink())

    def test_prejournal_receipt_rejects_child_replaced_after_inventory_validation(self):
        import macos.install_app as install_app_module
        from macos.install_app import _prepare_common_transaction
        source = self._source_fixture()
        record = record_python_runtime(str(Path(sys.executable).resolve()), tuple(sys.version_info[:2]))
        bundle = build_bundle(source, self.root / "receipt-race-build", record)
        applications = self.home / "Applications"
        applications.mkdir(mode=0o700)
        (applications / "Xserverメール通知管理.app").write_text("unsafe", encoding="utf-8")
        outside = self.root / "outside-file"
        outside.write_bytes(b"outside-must-survive")
        outside.chmod(0o600)
        real_create = install_app_module.create_cleanup_receipt

        def replace_child_before_receipt(*args, **kwargs):
            staged = applications / ".xserver-mail-lineworks-installer" / ("transaction." + "7" * 32) / "staged.app"
            target = staged / "Contents/Resources/manager/manage.py"
            target.unlink()
            shutil.copyfile(outside, target)
            target.chmod(0o600)
            return real_create(*args, **kwargs)

        with patch("macos.install_app.Path.home", return_value=self.home), \
                patch("macos.install_app.create_cleanup_receipt", side_effect=replace_child_before_receipt), \
                self.assertRaises(InstallError):
            _prepare_common_transaction(bundle, os.getuid(), "7" * 32)

        self.assertTrue(outside.exists())
        self.assertEqual(b"outside-must-survive", outside.read_bytes())
        transaction = applications / ".xserver-mail-lineworks-installer" / ("transaction." + "7" * 32)
        self.assertTrue(transaction.exists())
        self.assertFalse((applications / ".xserver-mail-lineworks-installer" / ("cleanup." + "7" * 32 + ".json")).exists())

    def test_fresh_process_recovery_cleans_verified_transaction_by_receipt(self):
        applications = self.home / "Applications"
        installer = applications / ".xserver-mail-lineworks-installer"
        txn_id = "b" * 32
        transaction = installer / f"transaction.{txn_id}"
        transaction.mkdir(parents=True, mode=0o700)
        transaction.chmod(0o700)
        installer.chmod(0o700)
        applications.chmod(0o700)
        fd = os.open(transaction, os.O_RDONLY | os.O_DIRECTORY)
        try:
            from macos.install_transaction import Journal
            Journal(fd, txn_id, rename=lambda d1, a, d2, b: os.rename(
                a, b, src_dir_fd=d1, dst_dir_fd=d2)).append("VERIFIED", {"tree_sha256": "0" * 64})
        finally:
            os.close(fd)
        applications_fd = os.open(applications, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch("macos.install_app.Path.home", return_value=self.home), \
                    self.assertRaises(InstallError):
                recover_transactions(applications_fd, os.getuid())
        finally:
            os.close(applications_fd)
        self.assertTrue(transaction.exists())
        self.assertFalse((installer / f"cleanup.{txn_id}.json").exists())

    def test_recovery_refuses_to_run_beside_an_active_transaction_lock(self):
        import fcntl
        applications = self.home / "Applications"
        installer = applications / ".xserver-mail-lineworks-installer"
        installer.mkdir(parents=True, mode=0o700)
        applications.chmod(0o700); installer.chmod(0o700)
        lock = os.open(installer / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        applications_fd = os.open(applications, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(InstallError, "another installation"):
                recover_transactions(applications_fd, os.getuid())
        finally:
            os.close(applications_fd); os.close(lock)

    def test_journal_rejects_unknown_and_abandoned_temp_entries(self):
        from macos.install_transaction import Journal, RecoveryError
        transaction = self.root / "journal"
        transaction.mkdir(mode=0o700)
        fd = os.open(transaction, os.O_RDONLY | os.O_DIRECTORY)
        try:
            journal = Journal(fd, "c" * 32, rename=lambda d1, a, d2, b: os.rename(
                a, b, src_dir_fd=d1, dst_dir_fd=d2))
            journal.append("PREPARED_NEW", {"tree_sha256": "0" * 64})
            for name in ("unknown", "state.99.tmp", "current.99.tmp", "state.bad.json"):
                (transaction / name).write_bytes(b"x")
                (transaction / name).chmod(0o600)
                with self.subTest(name=name), self.assertRaises(RecoveryError):
                    journal.recover_head()
                (transaction / name).unlink()
        finally:
            os.close(fd)

    def test_bootstrap_has_only_fixed_candidates_and_no_path_lookup(self):
        script = (Path(__file__).parents[2] / "macos" / "install_app.command").read_text()
        for version in ("3.14", "3.13"):
            self.assertIn(f"/Library/Frameworks/Python.framework/Versions/{version}/bin/python{version}", script)
            self.assertIn(f"/Library/Frameworks/Python.framework/Versions/{version}/bin/python3", script)
            self.assertIn(f"/opt/homebrew/bin/python{version}", script)
            self.assertIn(f"/usr/local/bin/python{version}", script)
        for forbidden in ("command -v", "which ", "/usr/bin/env python3", "PYTHON_PATH", "dirname"):
            self.assertNotIn(forbidden, script)
        candidates = __import__("re").findall(r"^\s*(/(?:Library|opt|usr)/\S*python(?:3|3[.]\d+))\s*\\?$", script, __import__("re").M)
        self.assertEqual([
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14",
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
            "/opt/homebrew/bin/python3.14", "/usr/local/bin/python3.14",
            "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13",
            "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
            "/opt/homebrew/bin/python3.13", "/usr/local/bin/python3.13",
        ], candidates)

    def test_bootstrap_checks_candidate_owner_mode_and_groups_with_fixed_tools(self):
        script = (Path(__file__).parents[2] / "macos" / "install_app.command").read_text()
        self.assertIn("/usr/bin/stat", script)
        self.assertIn("/usr/bin/id -u", script)
        self.assertIn("/usr/bin/id -G", script)
        self.assertNotIn(" stat ", script)
        self.assertNotIn(" id ", script)
        self.assertIn("trusted_python_candidate", script)
        self.assertRegex(script, r"/usr/bin/stat[^\n]+\|\| return 1")
        self.assertRegex(script, r"/usr/bin/id -u[^\n]+\|\| return 1")
        self.assertRegex(script, r"/usr/bin/id -G[^\n]+\|\| return 1")
        self.assertNotIn("$PATH", script)
        self.assertRegex(script, r"mode_value\s*&\s*3072")
        self.assertIn("[0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]", script)

    @unittest.skipUnless(sys.platform == "darwin", "macOS bootstrap integration test")
    def test_bootstrap_help_reaches_a_fixed_regular_framework_python(self):
        framework_candidates = [
            Path(f"/Library/Frameworks/Python.framework/Versions/{version}/bin/python{version}")
            for version in ("3.14", "3.13")
        ]
        if not any(path.is_file() and not path.is_symlink() for path in framework_candidates):
            self.skipTest("supported regular framework Python is not installed")
        repository = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as hostile:
            marker = Path(hostile) / "marker"
            for name in ("stat", "id", "python3"):
                fake = Path(hostile) / name
                fake.write_text(f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 99\n")
                fake.chmod(0o755)
            completed = subprocess.run(
                ["/bin/bash", str(repository / "macos/install_app.command"), "--help"],
                cwd=repository, text=True, capture_output=True, check=False,
                env={**os.environ, "PATH": hostile},
            )
            self.assertFalse(marker.exists())
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--source-root", completed.stdout)

    def _source_fixture(self):
        source = self.root / "source"
        (source / "macos").mkdir(parents=True, mode=0o700)
        (source / "manager").mkdir(mode=0o700)
        for directory in ("bin", "src", "vendor"):
            (source / directory).mkdir(mode=0o700)
        repository = Path(__file__).parents[2]
        for relative in (
            "macos/AppLauncher.applescript", "macos/runtime.py", "macos/local_config.py",
            "macos/launcher.sh", "manager/manage.py", "manager/keychain.py",
            "manager/ftps_deployer.py", "manager/xserver_api.py",
            "manager/remote_validator.py", "manager/release_deployer.py",
            "manager/release_workflow.py",
            "manager/scope_journal.py",
            "manager/private_config_ssh.py",
            "manager/email_address.py",
            "bin/manage-private-config.php", "bin/stable-mail-entrypoint.php",
            "bin/mail-forward-command-701.php", "bin/validate-release.php",
            "fixed-runtime/legacy-manifest.json",
            "fixed-runtime/generation-b9fd468-manifest.json",
            "src/ReleaseValidator.php", "vendor/autoload.php",
            "vendor/php-di/php-di/src/Compiler/Template.php",
        ):
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(repository / relative, destination)
            destination.chmod(0o700 if relative.endswith("launcher.sh") else 0o600)
        for directory in (source / "vendor").rglob("*"):
            if directory.is_dir():
                directory.chmod(0o700)
        source.chmod(0o700)
        return source


if __name__ == "__main__":
    unittest.main()
