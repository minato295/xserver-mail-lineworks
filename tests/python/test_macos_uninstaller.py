import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from macos.uninstall_app import UninstallError, uninstall


class UninstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.applications = self.home / "Applications"
        self.app = self.applications / "Xserverメール通知管理.app"
        self.config = self.home / "Library/Application Support/XserverMailLineworks/config.json"
        self.app.mkdir(parents=True, mode=0o700)
        from macos.install_app import EXPECTED_BUNDLE_FILES, _ALLOWED_DIRS
        for directory in sorted(_ALLOWED_DIRS - {"."}, key=lambda value: value.count("/")):
            (self.app / directory).mkdir(exist_ok=True, mode=0o700)
        for relative in EXPECTED_BUNDLE_FILES:
            path = self.app / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            path.chmod(0o700 if relative.endswith("launcher.sh") or relative == "Contents/MacOS/applet" else 0o600)
        (self.app / "Contents/Info.plist").write_bytes(
            __import__("plistlib").dumps({"CFBundleIdentifier": "jp.example.xserver-mail-lineworks-manager"}))
        (self.app / "Contents/Info.plist").chmod(0o600)
        self.config.parent.mkdir(parents=True, mode=0o700)
        self.config.write_bytes(b"config")
        self.config.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_removes_only_app_and_never_keychain(self):
        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            uninstall(uid=os.getuid())
        self.assertFalse(self.app.exists())
        self.assertTrue(self.config.exists())

    def test_keychain_removal_is_always_refused_without_mutation(self):
        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaisesRegex(UninstallError, "キーチェーン"):
                uninstall(remove_keychain=True, uid=os.getuid())
        self.assertTrue(self.app.exists())
        self.assertTrue(self.config.exists())

    def test_rejects_privileged_or_mismatched_identity_before_mutation(self):
        for real, effective in ((0, 0), (501, 0), (0, 501), (501, 502)):
            with self.subTest(real=real, effective=effective), \
                    patch("macos.uninstall_app.os.getuid", return_value=real), \
                    patch("macos.uninstall_app.os.geteuid", return_value=effective), \
                    patch("macos.uninstall_app.Path.home", return_value=self.home):
                with self.assertRaisesRegex(UninstallError, "管理者|UID"):
                    uninstall()
            self.assertTrue(self.app.exists())

    def test_rejects_app_without_fixed_bundle_identifier(self):
        (self.app / "Contents/Info.plist").write_bytes(
            __import__("plistlib").dumps({"CFBundleIdentifier": "evil.example"}))
        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaisesRegex(UninstallError, "bundle"):
                uninstall(uid=os.getuid())
        self.assertTrue(self.app.exists())

    def test_symlink_entry_stops_without_deleting_outside_or_app(self):
        outside = self.home / "outside"
        outside.write_bytes(b"keep")
        (self.app / "link").symlink_to(outside)
        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaisesRegex(UninstallError, "identity|manifest"):
                uninstall(uid=os.getuid())
        self.assertEqual(b"keep", outside.read_bytes())
        self.assertTrue(self.app.exists())

    def test_implementation_does_not_use_recursive_path_deletion(self):
        source = (Path(__file__).parents[2] / "macos/uninstall_app.py").read_text()
        self.assertNotIn("shutil.rmtree", source)

    def test_crash_after_trash_rename_is_completed_by_next_process(self):
        def crash(point, *_args):
            if point == "after_uninstall_trash_rename":
                raise RuntimeError("simulated process death")

        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaisesRegex(RuntimeError, "process death"):
                uninstall(uid=os.getuid(), _fault=crash)
            self.assertFalse(self.app.exists())
            uninstall(uid=os.getuid())
        installer = self.applications / ".xserver-mail-lineworks-installer"
        self.assertEqual({"lock"}, {p.name for p in installer.iterdir()})

    def test_recovery_refuses_replaced_trash_tree(self):
        def crash(point, *_args):
            if point == "after_uninstall_trash_rename":
                raise RuntimeError("simulated process death")
        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaises(RuntimeError):
                uninstall(uid=os.getuid(), _fault=crash)
            trash = next((self.applications / ".xserver-mail-lineworks-installer").glob("transaction.*/trash.app"))
            (trash / "attacker").write_bytes(b"replacement")
            with self.assertRaisesRegex(UninstallError, "identity|manifest"):
                uninstall(uid=os.getuid())
        self.assertTrue((trash / "attacker").exists())

    def test_lock_time_revalidation_rejects_app_replacement_after_initial_check(self):
        original = self.applications / "original.app"
        replaced = False
        def attack(*_args):
            nonlocal replaced
            if not replaced:
                self.app.rename(original)
                self.app.mkdir(mode=0o700)
                replaced = True
            return 0
        with patch("macos.uninstall_app.Path.home", return_value=self.home), \
                patch("macos.uninstall_app._recover_uninstall_transactions", side_effect=attack):
            with self.assertRaisesRegex(UninstallError, "manifest|bundle"):
                uninstall(uid=os.getuid())
        self.assertTrue(self.app.exists())
        self.assertTrue(original.exists())

    def test_crash_after_prepared_requires_fresh_uninstall_without_recovery_rename(self):
        def crash(point, *_args):
            if point == "after_uninstall_prepared":
                raise RuntimeError("simulated process death")

        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaises(RuntimeError):
                uninstall(uid=os.getuid(), _fault=crash)
            self.assertTrue(self.app.exists())
            before = os.lstat(self.app)
            victim = self.home / "victim"
            victim.write_bytes(b"must remain")
            victim_before = os.lstat(victim)
            with patch("macos.uninstall_app.rename_exclusive") as recovery_rename, \
                    self.assertRaisesRegex(UninstallError, "再実行"):
                uninstall(uid=os.getuid())
            recovery_rename.assert_not_called()
            after = os.lstat(self.app)
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            victim_after = os.lstat(victim)
            self.assertEqual((victim_before.st_dev, victim_before.st_ino, b"must remain"),
                             (victim_after.st_dev, victim_after.st_ino, victim.read_bytes()))
            self.assertFalse(any((self.applications / ".xserver-mail-lineworks-installer").glob("transaction.*")))
            uninstall(uid=os.getuid())
        self.assertFalse(self.app.exists())

    def test_prepared_recovery_rejects_public_app_replacement_before_rename(self):
        def crash(point, *_args):
            if point == "after_uninstall_prepared":
                raise RuntimeError("simulated process death")
        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaises(RuntimeError):
                uninstall(uid=os.getuid(), _fault=crash)
            target = self.app / "Contents/Resources/runtime.py"
            target.write_bytes(b"attacker replacement")
            target.chmod(0o600)
            before = target.read_bytes()
            with self.assertRaisesRegex(UninstallError, "identity|manifest"):
                uninstall(uid=os.getuid())
        self.assertTrue(self.app.exists())
        self.assertEqual(before, target.read_bytes())
        transaction = next((self.applications / ".xserver-mail-lineworks-installer").glob("transaction.*"))
        self.assertFalse((transaction / "trash.app").exists())

    def test_remove_config_intent_survives_crash_after_move(self):
        def crash(point, *_args):
            if point == "after_uninstall_trash_rename":
                raise RuntimeError("simulated process death")

        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaises(RuntimeError):
                uninstall(remove_config=True, uid=os.getuid(), _fault=crash)
            self.assertTrue(self.config.exists())
            uninstall(uid=os.getuid())
        self.assertFalse(self.config.exists())

    def test_cleanup_receipt_resumes_after_each_identity_bound_delete_crash(self):
        crashed = False

        def crash(kind, _path=None):
            nonlocal crashed
            if kind in {"file", "dir"} and not crashed:
                crashed = True
                raise RuntimeError("cleanup process death")

        with patch("macos.uninstall_app.Path.home", return_value=self.home):
            with self.assertRaisesRegex(RuntimeError, "cleanup process death"):
                uninstall(uid=os.getuid(), _fault=crash)
            uninstall(uid=os.getuid())
        installer = self.applications / ".xserver-mail-lineworks-installer"
        self.assertEqual({"lock"}, {p.name for p in installer.iterdir()})
