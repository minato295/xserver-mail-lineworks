import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from macos.local_config import LocalConfigError, load_config, to_environment, validate_config


VALID = {
    "servername": "example.xsrv.jp",
    "command_path": "/home/example/bin/run",
    "ftps_host": "sv.example.invalid",
    "config_path": "/config/app.json",
    "filesystem_home": "/home/example",
    "ssh_alias": "xserver-production",
}


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root
        self.library = root / "Library"
        self.support = self.library / "Application Support"
        self.app_dir = self.support / "XserverMailLineworks"
        self.app_dir.mkdir(parents=True)
        os.chmod(self.library, 0o755)
        os.chmod(self.support, 0o755)
        os.chmod(self.app_dir, 0o700)
        self.path = self.app_dir / "config.json"
        self.write(VALID)

        home_patch = mock.patch("macos.local_config.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def assert_rejected(self):
        with self.assertRaises(LocalConfigError):
            load_config(self.path, os.getuid())

    def test_validate_config_returns_copy_of_valid_config(self):
        config = validate_config(VALID)
        self.assertEqual(config, VALID)
        self.assertIsNot(config, VALID)

    def test_validate_config_rejects_unknown_key(self):
        with self.assertRaises(LocalConfigError):
            validate_config({**VALID, "secret": "do-not-leak"})

    def test_validate_config_rejects_invalid_server(self):
        with self.assertRaises(LocalConfigError):
            validate_config({**VALID, "servername": "example.invalid"})

    def test_validate_config_rejects_public_html_path(self):
        with self.assertRaises(LocalConfigError):
            validate_config({**VALID, "filesystem_home": "/home/PUBLIC_HTML/file"})

    def test_loads_valid_schema_and_maps_environment(self):
        config = load_config(self.path, os.getuid())
        self.assertEqual(config, VALID)
        self.assertEqual(to_environment(config), {
            "XSERVER_SERVERNAME": "example.xsrv.jp",
            "XSERVER_COMMAND_PATH": "/home/example/bin/run",
            "XSERVER_FTPS_HOST": "sv.example.invalid",
            "XSERVER_CONFIG_PATH": "/config/app.json",
            "XSERVER_HOME": "/home/example",
            "XSERVER_SSH_ALIAS": "xserver-production",
        })
        self.assertIsNot(config, to_environment(config))

    def test_ssh_alias_accepts_only_a_safe_ascii_alias_token(self):
        for alias in ("xserver", "xserver-prod_1", "office.server"):
            with self.subTest(alias=alias):
                self.assertEqual(alias, validate_config({**VALID, "ssh_alias": alias})["ssh_alias"])
        for alias in (
            "", "-option", "user@host", "host/name", "host:name", "two words",
            "host;command", "host$(command)", "ホスト", "host\nname", "*.example",
        ):
            with self.subTest(alias=alias), self.assertRaises(LocalConfigError):
                validate_config({**VALID, "ssh_alias": alias})

    def test_rejects_noncanonical_absolute_location_and_component_names(self):
        for path in (
            Path("Library/Application Support/XserverMailLineworks/config.json"),
            self.home / "library" / "Application Support" / "XserverMailLineworks" / "config.json",
            self.home / "Library" / "ApplicationSupport" / "XserverMailLineworks" / "config.json",
            self.home / "Library" / "Application Support" / "Xserver Mail LINE WORKS" / "config.json",
            self.home / "Library" / "Application Support" / "XserverMailLineworks" / "launcher.json",
        ):
            with self.subTest(path=path), self.assertRaises(LocalConfigError):
                load_config(path, os.getuid())

    def test_rejects_duplicate_json_key(self):
        body = json.dumps(VALID)[:-1] + ',"servername":"other.xsrv.jp"}'
        self.path.write_text(body, encoding="utf-8")
        os.chmod(self.path, 0o600)
        self.assert_rejected()

    def test_rejects_unknown_key(self):
        self.write({**VALID, "secret": "do-not-leak"})
        self.assert_rejected()

    def test_rejects_missing_key(self):
        value = dict(VALID)
        del value["servername"]
        self.write(value)
        self.assert_rejected()

    def test_rejects_non_object_json(self):
        self.write(["not", "an", "object"])
        self.assert_rejected()

    def test_rejects_non_string_and_empty_values(self):
        for value in (None, 1, ""):
            with self.subTest(value=value):
                self.write({**VALID, "ftps_host": value})
                self.assert_rejected()

    def test_rejects_symlink_file(self):
        target = self.app_dir / "target"
        self.path.replace(target)
        self.path.symlink_to(target)
        self.assert_rejected()

    def test_rejects_symlink_app_parent(self):
        real = self.support / "real-app"
        self.app_dir.rename(real)
        self.app_dir.symlink_to(real, target_is_directory=True)
        self.assert_rejected()

    def test_rejects_symlink_library_or_application_support(self):
        for directory in (self.library, self.support):
            with self.subTest(directory=directory.name):
                target = directory.with_name(directory.name + "-real")
                directory.rename(target)
                directory.symlink_to(target, target_is_directory=True)
                self.assert_rejected()
                directory.unlink()
                target.rename(directory)

    def test_rejects_file_mode_0640(self):
        os.chmod(self.path, 0o640)
        self.assert_rejected()

    def test_rejects_app_directory_mode_0755(self):
        os.chmod(self.app_dir, 0o755)
        self.assert_rejected()

    def test_rejects_writable_library_or_application_support(self):
        for directory in (self.library, self.support):
            with self.subTest(directory=directory.name):
                os.chmod(directory, 0o777)
                self.assert_rejected()
                os.chmod(directory, 0o755)

    def test_rejects_wrong_directory_uid(self):
        real_fstat = os.fstat

        def wrong_uid(fd):
            result = real_fstat(fd)
            if stat.S_ISDIR(result.st_mode) and result.st_ino == self.support.stat().st_ino:
                values = list(result)
                values[stat.ST_UID] = result.st_uid + 1
                return os.stat_result(values)
            return result

        with mock.patch("macos.local_config.os.fstat", side_effect=wrong_uid):
            self.assert_rejected()

    def test_rejects_directory_device_mode_or_type_change(self):
        real_fstat = os.fstat
        support_inode = self.support.stat().st_ino
        for field, replacement in (
            (stat.ST_DEV, self.support.stat().st_dev + 1),
            (stat.ST_MODE, stat.S_IFREG | 0o755),
            (stat.ST_MODE, stat.S_IFDIR | 0o777),
        ):
            with self.subTest(field=field, replacement=replacement):
                def changed(fd, field=field, replacement=replacement):
                    result = real_fstat(fd)
                    if stat.S_ISDIR(result.st_mode) and result.st_ino == support_inode:
                        values = list(result)
                        values[field] = replacement
                        return os.stat_result(values)
                    return result
                with mock.patch("macos.local_config.os.fstat", side_effect=changed):
                    self.assert_rejected()

    def test_parent_replacement_after_open_reads_only_from_original_dirfd_chain(self):
        replacement = self.support.with_name("replacement-support")
        replacement_app = replacement / "XserverMailLineworks"
        replacement_app.mkdir(parents=True)
        os.chmod(replacement, 0o755)
        os.chmod(replacement_app, 0o700)
        replacement_path = replacement_app / "config.json"
        replacement_path.write_text(json.dumps({**VALID, "servername": "evil.xsrv.jp"}), encoding="utf-8")
        os.chmod(replacement_path, 0o600)

        real_open = os.open
        replaced = False

        def replace_parent(path, flags, *args, **kwargs):
            nonlocal replaced
            fd = real_open(path, flags, *args, **kwargs)
            if path == "Application Support" and not replaced:
                original = self.support.with_name("original-support")
                self.support.rename(original)
                replacement.rename(self.support)
                replaced = True
            return fd

        with mock.patch("macos.local_config.os.open", side_effect=replace_parent):
            self.assertEqual(load_config(self.path, os.getuid()), VALID)
        self.assertTrue(replaced)

    def test_accepts_short_reads_from_same_file_descriptor(self):
        real_read = os.read
        with mock.patch(
            "macos.local_config.os.read",
            side_effect=lambda fd, size: real_read(fd, min(size, 7)),
        ):
            self.assertEqual(load_config(self.path, os.getuid()), VALID)

    def test_rejects_wrong_file_uid_without_leaking_value(self):
        real_stat = os.stat
        def wrong_uid(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == "config.json":
                values = list(result)
                values[stat.ST_UID] = result.st_uid + 1
                return os.stat_result(values)
            return result
        with mock.patch("macos.local_config.os.stat", side_effect=wrong_uid):
            with self.assertRaises(LocalConfigError) as caught:
                load_config(self.path, os.getuid())
        self.assertNotIn("do-not-leak", str(caught.exception))

    def test_rejects_oversize_file(self):
        self.path.write_bytes(b" " * 65537)
        os.chmod(self.path, 0o600)
        self.assert_rejected()

    def test_rejects_malformed_json_without_echoing_input(self):
        self.path.write_text('{"password":"do-not-leak"', encoding="utf-8")
        os.chmod(self.path, 0o600)
        with self.assertRaises(LocalConfigError) as caught:
            load_config(self.path, os.getuid())
        self.assertNotIn("do-not-leak", str(caught.exception))

    def test_rejects_control_characters(self):
        for bad in ("line\nbreak", "nul\0byte", "tab\tvalue", "delete\x7fvalue"):
            with self.subTest(value=repr(bad)):
                self.write({**VALID, "filesystem_home": "/home/" + bad})
                self.assert_rejected()

    def test_rejects_invalid_hosts(self):
        for key, values in {
            "servername": ["example.invalid", "https://example.xsrv.jp", "x.xsrv.jp/path"],
            "ftps_host": ["https://host.example.invalid", "user@localhost", "host.example.invalid:21", "host/path"],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.write({**VALID, key: value})
                    self.assert_rejected()

    def test_rejects_relative_dot_and_public_html_paths(self):
        for key in ("command_path", "config_path", "filesystem_home"):
            for value in ("relative/path", "/safe/../escape", "/home/PUBLIC_HTML/file"):
                with self.subTest(key=key, value=value):
                    self.write({**VALID, key: value})
                    self.assert_rejected()

    def test_rejects_inode_change_between_lstat_and_open(self):
        real_fstat = os.fstat
        def changed(fd):
            result = real_fstat(fd)
            values = list(result)
            values[stat.ST_INO] = result.st_ino + 1
            return os.stat_result(values)
        with mock.patch("macos.local_config.os.fstat", side_effect=changed):
            self.assert_rejected()

    def test_rejects_file_device_uid_mode_or_type_change_during_open(self):
        real_fstat = os.fstat
        original = self.path.stat()
        for field, replacement in (
            (stat.ST_DEV, original.st_dev + 1),
            (stat.ST_UID, original.st_uid + 1),
            (stat.ST_MODE, stat.S_IFREG | 0o640),
            (stat.ST_MODE, stat.S_IFDIR | 0o600),
        ):
            with self.subTest(field=field, replacement=replacement):
                def changed(fd, field=field, replacement=replacement):
                    result = real_fstat(fd)
                    if stat.S_ISREG(result.st_mode):
                        values = list(result)
                        values[field] = replacement
                        return os.stat_result(values)
                    return result
                with mock.patch("macos.local_config.os.fstat", side_effect=changed):
                    self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
