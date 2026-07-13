import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from macos.runtime import (
    RuntimeRecord,
    _validate_runtime_trust,
    record_python_runtime,
    run_manager,
    validate_python_runtime,
    validate_runtime_record,
)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.info = SimpleNamespace(st_dev=10, st_ino=20)
        self.record = RuntimeRecord("/opt/python3", 10, 20, 3, 13)

    def test_supported_versions_are_accepted(self):
        validate_runtime_record(self.record, self.info, (3, 13))
        validate_runtime_record(
            RuntimeRecord("/opt/python3", 10, 20, 3, 14), self.info, (3, 14)
        )

    def test_record_python_runtime_accepts_python_313_and_314(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory).resolve() / "python"
            executable.write_bytes(b"python")

            for version in ((3, 13), (3, 14)):
                with self.subTest(version=version):
                    record = record_python_runtime(str(executable), version)
                    self.assertEqual(str(executable), record.path)
                    self.assertEqual(version, (record.major, record.minor))

    def test_record_python_runtime_rejects_unsupported_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory).resolve() / "python"
            executable.write_bytes(b"python")

            for version in ((3, 12), (3, 15)):
                with self.subTest(version=version), self.assertRaises(RuntimeError):
                    record_python_runtime(str(executable), version)

    def test_record_python_runtime_rejects_symlink_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "python"
            executable.write_bytes(b"python")
            link = root / "python-link"
            link.symlink_to(executable)

            for candidate in (str(link), "python"):
                with self.subTest(candidate=candidate), self.assertRaises(RuntimeError):
                    record_python_runtime(candidate, (3, 13))

    def test_record_python_runtime_rejects_bool_version_components(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory).resolve() / "python"
            executable.write_bytes(b"python")

            for version in ((True, 13), (3, True)):
                with self.subTest(version=version), self.assertRaises(RuntimeError):
                    record_python_runtime(str(executable), version)

    def test_record_python_runtime_records_lstat_device_and_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory).resolve() / "python"
            executable.write_bytes(b"python")
            info = os.lstat(executable)

            record = record_python_runtime(str(executable), (3, 13))

            self.assertEqual((info.st_dev, info.st_ino), (record.device, record.inode))

    def test_runtime_trust_rejects_wrong_owner_and_writable_permissions(self):
        base = os.lstat(__file__)
        variants = (
            {"st_uid": os.getuid() + 1000},
            {"st_mode": stat.S_IFREG | 0o777},
            {"st_mode": stat.S_IFREG | 0o775, "st_gid": 4242},
        )
        for changes in variants:
            values = {name: getattr(base, name) for name in
                      ("st_mode", "st_uid", "st_gid", "st_dev", "st_ino")}
            values.update(changes)
            fake = SimpleNamespace(**values)
            groups = [4242] if changes.get("st_gid") == 4242 else []
            with self.subTest(changes=changes), \
                 patch("macos.runtime.os.getgroups", return_value=groups), \
                 patch("macos.runtime.os.getgid", return_value=9999), \
                 self.assertRaises(RuntimeError):
                _validate_runtime_trust(fake)

    def test_runtime_trust_rejects_setuid_and_setgid_for_root_or_current_owner(self):
        base = os.lstat(__file__)
        for owner, mode in ((0, 0o4755), (os.getuid(), 0o2755)):
            fake = SimpleNamespace(
                st_mode=stat.S_IFREG | mode, st_uid=owner, st_gid=4242,
                st_dev=base.st_dev, st_ino=base.st_ino,
            )
            with self.subTest(owner=owner, mode=oct(mode)), \
                 patch("macos.runtime.os.getgroups", return_value=[]), \
                 patch("macos.runtime.os.getgid", return_value=9999), \
                 self.assertRaises(RuntimeError):
                _validate_runtime_trust(fake)

    def test_runtime_trust_allows_root_owner_and_nonmember_group_writable(self):
        base = os.lstat(__file__)
        fake = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o775, st_uid=0, st_gid=0,
            st_dev=base.st_dev, st_ino=base.st_ino,
        )
        with patch("macos.runtime.os.getgroups", return_value=[20, 80]), \
             patch("macos.runtime.os.getgid", return_value=20):
            _validate_runtime_trust(fake)

    def test_runtime_trust_fails_closed_when_group_lookup_fails(self):
        with patch("macos.runtime.os.getgroups", side_effect=OSError("failure")), \
             self.assertRaises(RuntimeError):
            _validate_runtime_trust(os.lstat(__file__))

    def test_validate_python_runtime_rechecks_permissions_not_only_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory).resolve() / "python"
            executable.write_bytes(b"python")
            executable.chmod(0o755)
            info = os.lstat(executable)
            metadata = Path(directory) / "python-runtime.json"
            metadata.write_text(json.dumps({
                "path": str(executable), "device": info.st_dev, "inode": info.st_ino,
                "major": 3, "minor": 13,
            }), encoding="utf-8")
            executable.chmod(0o757)
            with patch("macos.runtime.sys.version_info", (3, 13)), self.assertRaises(RuntimeError):
                validate_python_runtime(metadata, str(executable))

    def test_record_python_runtime_errors_do_not_expose_inputs(self):
        secret_path = "/secret/customer/runtime/python"
        secret_version = (9, 99)

        with self.assertRaises(RuntimeError) as caught:
            record_python_runtime(secret_path, secret_version)

        message = str(caught.exception)
        self.assertNotIn(secret_path, message)
        self.assertNotIn("9", message)
        self.assertNotIn("99", message)

    def test_version_and_file_identity_mismatches_are_rejected(self):
        variants = [
            (RuntimeRecord("/opt/python3", 10, 20, 3, 12), self.info, (3, 12)),
            (self.record, self.info, (3, 14)),
            (self.record, SimpleNamespace(st_dev=11, st_ino=20), (3, 13)),
            (self.record, SimpleNamespace(st_dev=10, st_ino=21), (3, 13)),
        ]
        for record, info, version in variants:
            with self.subTest(record=record, version=version), self.assertRaises(RuntimeError):
                validate_runtime_record(record, info, version)

    def test_metadata_requires_exact_schema_types_and_no_duplicates(self):
        invalid = [
            '{"path":"/x","device":1,"inode":2,"major":3,"minor":13,"extra":1}',
            '{"path":"/x","device":1,"inode":2,"major":3}',
            '{"path":"/x","device":1,"inode":2,"major":3,"minor":13,"minor":13}',
            '{"path":"/x","device":true,"inode":2,"major":3,"minor":13}',
        ]
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory).resolve() / "python"
            executable.write_bytes(b"python")
            metadata = Path(directory) / "python-runtime.json"
            for value in invalid:
                metadata.write_text(value, encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    validate_python_runtime(metadata, executable=str(executable))

    def test_runtime_requires_canonical_executable_path(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            executable = Path(directory) / "python"
            executable.write_bytes(b"python")
            info = executable.stat()
            metadata = Path(directory) / "python-runtime.json"
            metadata.write_text(json.dumps({
                "path": str(executable.resolve()), "device": info.st_dev, "inode": info.st_ino,
                "major": 3, "minor": 13,
            }), encoding="utf-8")
            with patch("macos.runtime.sys.version_info", (3, 13)):
                self.assertEqual(str(executable.resolve()), validate_python_runtime(metadata, str(executable)).path)
            link = Path(directory) / "python-link"
            link.symlink_to(executable)
            with patch("macos.runtime.sys.version_info", (3, 13)), self.assertRaises(RuntimeError):
                validate_python_runtime(metadata, str(link))

    def test_runtime_rejects_metadata_symlink_and_recorded_path_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "python"
            executable.write_bytes(b"python")
            info = executable.stat()
            metadata = root / "python-runtime.json"
            record = {
                "path": str(executable), "device": info.st_dev, "inode": info.st_ino,
                "major": 3, "minor": 13,
            }
            metadata.write_text(json.dumps(record), encoding="utf-8")
            metadata_link = root / "python-runtime-link.json"
            metadata_link.symlink_to(metadata)
            with patch("macos.runtime.sys.version_info", (3, 13)), self.assertRaises(RuntimeError):
                validate_python_runtime(metadata_link, str(executable))

            record["path"] = str(root / "different-python")
            metadata.write_text(json.dumps(record), encoding="utf-8")
            with patch("macos.runtime.sys.version_info", (3, 13)), self.assertRaises(RuntimeError):
                validate_python_runtime(metadata, str(executable))

    def test_manager_receives_merged_environment_without_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory).resolve() / "Resources"
            manager = resources / "manager"
            manager.mkdir(parents=True)
            (manager / "manage.py").write_text("", encoding="utf-8")
            config = Path(directory).resolve() / "config.json"
            config.write_text("{}", encoding="utf-8")
            calls = []

            def runner(argv, **kwargs):
                calls.append((argv, kwargs))
                return SimpleNamespace(returncode=7)

            with patch("macos.runtime.load_config", return_value={"servername": "server.example.invalid"}), \
                 patch("macos.runtime.to_environment", return_value={"XSERVER_SERVERNAME": "server.example.invalid"}), \
                 patch.dict(os.environ, {"KEEP_ME": "yes"}, clear=True):
                result = run_manager(resources, config, runner=runner)

            self.assertEqual(7, result)
            argv, kwargs = calls[0]
            self.assertEqual([os.sys.executable, "-B", str(manager / "manage.py")], argv)
            self.assertEqual("yes", kwargs["env"]["KEEP_ME"])
            self.assertEqual("server.example.invalid", kwargs["env"]["XSERVER_SERVERNAME"])
            self.assertEqual("1", kwargs["env"]["PYTHONDONTWRITEBYTECODE"])
            self.assertEqual(False, kwargs["check"])
            self.assertEqual(False, kwargs["shell"])

    def test_manager_launch_disables_bytecode_even_if_parent_environment_allows_it(self):
        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory).resolve() / "Resources"
            manager = resources / "manager"
            manager.mkdir(parents=True)
            (manager / "manage.py").write_text("", encoding="utf-8")
            config = Path(directory).resolve() / "config.json"
            config.write_text("{}", encoding="utf-8")
            calls = []

            def runner(argv, **kwargs):
                calls.append((argv, kwargs))
                return SimpleNamespace(returncode=0)

            with patch("macos.runtime.load_config", return_value={}), \
                 patch("macos.runtime.to_environment", return_value={}), \
                 patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "0"}, clear=True):
                self.assertEqual(0, run_manager(resources, config, runner=runner))

            argv, kwargs = calls[0]
            self.assertEqual([os.sys.executable, "-B", str(manager / "manage.py")], argv)
            self.assertEqual("1", kwargs["env"]["PYTHONDONTWRITEBYTECODE"])

    def test_real_manager_subprocess_import_does_not_write_into_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory).resolve() / "Resources"
            manager = resources / "manager"
            manager.mkdir(parents=True)
            (manager / "probe.py").write_text("VALUE = 1\n", encoding="utf-8")
            (manager / "manage.py").write_text(
                "import probe\nassert probe.VALUE == 1\n",
                encoding="utf-8",
            )
            config = Path(directory).resolve() / "config.json"
            config.write_text("{}", encoding="utf-8")

            with patch("macos.runtime.load_config", return_value={}), \
                 patch("macos.runtime.to_environment", return_value={}), \
                 patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "0"}, clear=True):
                self.assertEqual(0, run_manager(resources, config))

            self.assertEqual([], list(resources.rglob("__pycache__")))
            self.assertEqual([], list(resources.rglob("*.pyc")))

    def test_manager_resources_and_config_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            resources = root / "Resources"
            (resources / "manager").mkdir(parents=True)
            (resources / "manager" / "manage.py").write_text("", encoding="utf-8")
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            links = []
            resources_link = root / "resources-link"; resources_link.symlink_to(resources); links.append((resources_link, config))
            config_link = root / "config-link"; config_link.symlink_to(config); links.append((resources, config_link))
            for resources_arg, config_arg in links:
                with self.subTest(resources=resources_arg, config=config_arg), self.assertRaises(RuntimeError):
                    run_manager(resources_arg, config_arg, runner=lambda *a, **k: None)

            (resources / "manager" / "manage.py").unlink()
            (resources / "manager" / "manage.py").symlink_to(root / "outside-manager.py")
            with self.assertRaises(RuntimeError):
                run_manager(resources, config, runner=lambda *a, **k: None)


if __name__ == "__main__":
    unittest.main()
