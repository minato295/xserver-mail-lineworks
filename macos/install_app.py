"""Build and validate the unprivileged macOS launcher bundle."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import fcntl
import hashlib
from enum import Enum
from dataclasses import asdict
from pathlib import Path

try:
    from .local_config import validate_config, load_config_with_digest
    from .runtime import RuntimeRecord, record_python_runtime
except ImportError:
    from local_config import validate_config, load_config_with_digest
    from runtime import RuntimeRecord, record_python_runtime

try:
    from .install_transaction import (
        create_bundle_manifest, copy_manifest_tree, rename_exclusive, rename_swap,
        create_cleanup_receipt, resume_cleanup, Journal,
        capture_cleanup_inventory,
        tree_digest_fd, classify_config_state, write_completion_receipt,
        verify_latest_completion_receipt,
    )
except ImportError:
    from install_transaction import (
        create_bundle_manifest, copy_manifest_tree, rename_exclusive, rename_swap,
        create_cleanup_receipt, resume_cleanup, Journal,
        capture_cleanup_inventory,
        tree_digest_fd, classify_config_state, write_completion_receipt,
        verify_latest_completion_receipt,
    )


BUNDLE_IDENTIFIER = "jp.example.xserver-mail-lineworks-manager"
_FIXED_GENERATION_MANIFEST_SIZE = 84003
_FIXED_GENERATION_MANIFEST_SHA256 = \
    "7a07310945682c0fc35b7b77260cbc2ddfd6a3f48ae5885e55bf7247292f5adb"
EXPECTED_BUNDLE_FILES = {
    "Contents/_CodeSignature/CodeResources",
    "Contents/Info.plist",
    "Contents/MacOS/applet",
    "Contents/PkgInfo",
    "Contents/Resources/Scripts/main.scpt",
    "Contents/Resources/applet.rsrc",
    "Contents/Resources/launcher.sh",
    "Contents/Resources/local_config.py",
    "Contents/Resources/runtime.py",
    "Contents/Resources/python-path",
    "Contents/Resources/python-runtime.json",
    "Contents/Resources/manager/manage.py",
    "Contents/Resources/manager/keychain.py",
    "Contents/Resources/manager/ftps_deployer.py",
    "Contents/Resources/manager/xserver_api.py",
    "Contents/Resources/manager/remote_validator.py",
    "Contents/Resources/manager/release_deployer.py",
    "Contents/Resources/manager/release_workflow.py",
    "Contents/Resources/manager/scope_journal.py",
    "Contents/Resources/manager/private_config_ssh.py",
    "Contents/Resources/manager/email_address.py",
    "Contents/Resources/fixed-runtime/manage-private-config.php",
    "Contents/Resources/fixed-runtime/legacy-manifest.json",
    "Contents/Resources/fixed-runtime/generation-b9fd468-manifest.json",
}
_PRE_GENERATION_ASSET_BUNDLE_FILES = frozenset(
    EXPECTED_BUNDLE_FILES - {
        "Contents/Resources/fixed-runtime/generation-b9fd468-manifest.json",
    }
)
_PRE_BOOTSTRAP_ASSET_BUNDLE_FILES = frozenset(
    EXPECTED_BUNDLE_FILES - {
        "Contents/Resources/fixed-runtime/manage-private-config.php",
        "Contents/Resources/fixed-runtime/legacy-manifest.json",
        "Contents/Resources/fixed-runtime/generation-b9fd468-manifest.json",
    }
)
_LEGACY_BUNDLE_FILES_WITHOUT_PRIVATE_CONFIG_SSH = frozenset(
    _PRE_BOOTSTRAP_ASSET_BUNDLE_FILES - {"Contents/Resources/manager/private_config_ssh.py"}
)
_LEGACY_BUNDLE_FILES_WITHOUT_EMAIL_ADDRESS = frozenset(
    _PRE_BOOTSTRAP_ASSET_BUNDLE_FILES - {"Contents/Resources/manager/email_address.py"}
)
_LEGACY_BUNDLE_FILES_WITHOUT_PRIVATE_CONFIG_SSH_OR_EMAIL_ADDRESS = frozenset(
    _PRE_BOOTSTRAP_ASSET_BUNDLE_FILES - {
        "Contents/Resources/manager/private_config_ssh.py",
        "Contents/Resources/manager/email_address.py",
    }
)
_EXISTING_DESTINATION_FILE_LAYOUTS = (
    frozenset(EXPECTED_BUNDLE_FILES),
    _PRE_GENERATION_ASSET_BUNDLE_FILES,
    _PRE_BOOTSTRAP_ASSET_BUNDLE_FILES,
    _LEGACY_BUNDLE_FILES_WITHOUT_EMAIL_ADDRESS,
    _LEGACY_BUNDLE_FILES_WITHOUT_PRIVATE_CONFIG_SSH,
    _LEGACY_BUNDLE_FILES_WITHOUT_PRIVATE_CONFIG_SSH_OR_EMAIL_ADDRESS,
)
_SOURCE_FILES = {
    "macos/runtime.py": "Contents/Resources/runtime.py",
    "macos/local_config.py": "Contents/Resources/local_config.py",
    "macos/launcher.sh": "Contents/Resources/launcher.sh",
    "manager/manage.py": "Contents/Resources/manager/manage.py",
    "manager/keychain.py": "Contents/Resources/manager/keychain.py",
    "manager/ftps_deployer.py": "Contents/Resources/manager/ftps_deployer.py",
    "manager/xserver_api.py": "Contents/Resources/manager/xserver_api.py",
    "manager/remote_validator.py": "Contents/Resources/manager/remote_validator.py",
    "manager/release_deployer.py": "Contents/Resources/manager/release_deployer.py",
    "manager/release_workflow.py": "Contents/Resources/manager/release_workflow.py",
    "manager/scope_journal.py": "Contents/Resources/manager/scope_journal.py",
    "manager/private_config_ssh.py": "Contents/Resources/manager/private_config_ssh.py",
    "manager/email_address.py": "Contents/Resources/manager/email_address.py",
}
_EXECUTABLE_BUNDLE_FILES = {
    "Contents/MacOS/applet",
    "Contents/Resources/launcher.sh",
}
_ALLOWED_DIRS = {
    ".", "Contents", "Contents/_CodeSignature", "Contents/MacOS", "Contents/Resources",
    "Contents/Resources/Scripts", "Contents/Resources/manager",
    "Contents/Resources/fixed-runtime",
}
_COMPILER_REQUIRED_FILES = {
    "Contents/Info.plist", "Contents/MacOS/applet", "Contents/PkgInfo",
    "Contents/Resources/Scripts/main.scpt", "Contents/Resources/applet.rsrc",
}
_COMPILER_OPTIONAL_FILES = {
    "Contents/Resources/Assets.car", "Contents/Resources/applet.icns",
    "Contents/_CodeSignature/CodeResources",
}
_COMPILER_REQUIRED_DIRS = {".", "Contents", "Contents/MacOS", "Contents/Resources", "Contents/Resources/Scripts"}
_COMPILER_OPTIONAL_DIRS = {"Contents/_CodeSignature"}


class InstallError(RuntimeError):
    pass


class InstallOutcome(Enum):
    NEW_INSTALLED = "NEW_INSTALLED"
    UPDATED = "UPDATED"


def _fail(reason: str) -> InstallError:
    return InstallError(f"Installer rejected: {reason}")


def ensure_unprivileged(real_uid: int | None = None, effective_uid: int | None = None) -> None:
    real_uid = os.getuid() if real_uid is None else real_uid
    effective_uid = os.geteuid() if effective_uid is None else effective_uid
    if real_uid == 0 or effective_uid == 0 or real_uid != effective_uid:
        raise _fail("privileged execution")


def validate_installer_python(version_info: tuple[int, int], executable: str) -> RuntimeRecord:
    try:
        return record_python_runtime(executable, version_info)
    except RuntimeError as exc:
        raise _fail("unsupported Python runtime") from exc


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), stat.S_IFMT(info.st_mode)


def _check_directory(info: os.stat_result, uid: int, description: str, exact: int | None = None) -> None:
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or (mode != exact if exact is not None else bool(mode & 0o022)):
        raise _fail(f"unsafe {description}")


def _open_dir(name: str | Path, uid: int, description: str, *, dir_fd: int | None = None, exact: int | None = None) -> int:
    try:
        before = os.lstat(name) if dir_fd is None else os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            raise _fail(f"changed {description}")
        _check_directory(after, uid, description, exact)
        return fd
    except InstallError:
        if "fd" in locals():
            os.close(fd)
        raise
    except OSError as exc:
        raise _fail(f"cannot open {description}") from exc


class ConfigTransaction:
    def __init__(self, app_fd: int, backup_name: str | None, marker_name: str | None = None,
                 txn_id: str | None = None):
        self._app_fd = app_fd
        self._backup_name = backup_name
        self._marker_name = marker_name
        self.txn_id = txn_id
        self._finished = False

    def _start_finish(self) -> int:
        if self._finished:
            raise _fail("transaction already finished")
        self._finished = True
        return self._app_fd

    def commit(self) -> None:
        fd = self._start_finish()
        try:
            if self._backup_name is not None:
                os.unlink(self._backup_name, dir_fd=fd)
            if self._marker_name is not None:
                os.unlink(self._marker_name, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
            self._app_fd = -1

    def rollback(self) -> None:
        fd = self._start_finish()
        try:
            try:
                os.unlink("config.json", dir_fd=fd)
            except FileNotFoundError:
                pass
            if self._backup_name is not None:
                os.rename(self._backup_name, "config.json", src_dir_fd=fd, dst_dir_fd=fd)
            if self._marker_name is not None:
                os.unlink(self._marker_name, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
            self._app_fd = -1


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _write_private_file(directory_fd: int, name: str, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=directory_fd)
    try:
        _write_all(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_config_atomic(path: Path, values: object, uid: int, *, txn_id: str | None = None,
                        before_publish=None) -> ConfigTransaction:
    checked = validate_config(values)
    path = Path(path)
    expected = Path.home() / "Library" / "Application Support" / "XserverMailLineworks" / "config.json"
    if not path.is_absolute() or path != expected:
        raise _fail("invalid configuration path")
    data = (json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fds: list[int] = []
    app_fd = -1
    if txn_id is not None and (len(txn_id) != 32 or any(c not in "0123456789abcdef" for c in txn_id)):
        raise _fail("invalid transaction id")
    temp_name = ".config-" + (txn_id or secrets.token_hex(16))
    marker_name = f".transaction-{txn_id}.json" if txn_id else None
    backup_name: str | None = None
    installed = False
    try:
        home_fd = _open_dir(Path.home(), uid, "home directory"); fds.append(home_fd)
        library_fd = _open_dir("Library", uid, "Library directory", dir_fd=home_fd); fds.append(library_fd)
        support_fd = _open_dir("Application Support", uid, "support directory", dir_fd=library_fd); fds.append(support_fd)
        try:
            os.mkdir("XserverMailLineworks", 0o700, dir_fd=support_fd)
            os.fsync(support_fd)
        except FileExistsError:
            pass
        app_fd = _open_dir("XserverMailLineworks", uid, "application directory", dir_fd=support_fd, exact=0o700)
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=app_fd)
        try:
            _write_all(temp_fd, data)
            os.fsync(temp_fd)
            os.fchmod(temp_fd, 0o600)
        finally:
            os.close(temp_fd)
        try:
            existing = os.stat("config.json", dir_fd=app_fd, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != uid or stat.S_IMODE(existing.st_mode) != 0o600:
                raise _fail("unsafe existing configuration")
            backup_name = ".backup-" + secrets.token_hex(16)
        except FileNotFoundError:
            pass
        if marker_name is not None:
            old_sha256 = None
            if backup_name is not None:
                old_fd = os.open("config.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=app_fd)
                try:
                    old_body = b""
                    while chunk := os.read(old_fd, 65536):
                        old_body += chunk
                finally:
                    os.close(old_fd)
                old_sha256 = __import__("hashlib").sha256(old_body).hexdigest()
            marker_payload = {
                "schema": 1, "txn_id": txn_id, "phase": "TXN_PREPARED",
                "temp": temp_name, "backup": backup_name, "trash": f".trash-{txn_id}",
                "old_config": backup_name is not None,
                "old_sha256": old_sha256,
                "new_sha256": __import__("hashlib").sha256(data).hexdigest(),
            }
            _write_private_file(app_fd, marker_name,
                                (json.dumps(marker_payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
            os.fsync(app_fd)
            if before_publish is not None:
                before_publish(dict(marker_payload))
        if backup_name is not None:
            os.rename("config.json", backup_name, src_dir_fd=app_fd, dst_dir_fd=app_fd)
        os.rename(temp_name, "config.json", src_dir_fd=app_fd, dst_dir_fd=app_fd)
        installed = True
        os.fsync(app_fd)
        transaction = ConfigTransaction(app_fd, backup_name, marker_name, txn_id)
        app_fd = -1
        return transaction
    except Exception:
        if app_fd >= 0:
            try:
                if installed:
                    os.unlink("config.json", dir_fd=app_fd)
                else:
                    os.unlink(temp_name, dir_fd=app_fd)
            except FileNotFoundError:
                pass
            if backup_name is not None:
                try:
                    os.rename(backup_name, "config.json", src_dir_fd=app_fd, dst_dir_fd=app_fd)
                except FileNotFoundError:
                    pass
            if marker_name is not None:
                try:
                    os.unlink(marker_name, dir_fd=app_fd)
                except FileNotFoundError:
                    pass
            os.fsync(app_fd)
        raise
    finally:
        if app_fd >= 0:
            os.close(app_fd)
        for fd in reversed(fds):
            os.close(fd)


def write_config_with_confirmation(path: Path, values: object, answer: str, uid: int) -> bool:
    if Path(path).exists() and answer != "上書きします":
        return False
    transaction = write_config_atomic(path, values, uid)
    transaction.commit()
    return True


def _trusted_source_file(source_root: Path, relative: str, uid: int) -> bytes:
    path = source_root / relative
    try:
        current = source_root
        for component in Path(relative).parts[:-1]:
            current /= component
            directory = os.lstat(current)
            _check_directory(directory, uid, "source directory")
            if stat.S_ISLNK(directory.st_mode):
                raise _fail("unsafe source directory")
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_uid != uid or stat.S_IMODE(before.st_mode) & 0o022:
            raise _fail("unsafe source file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            after = os.fstat(fd)
            if _identity(before) != _identity(after):
                raise _fail("source file changed")
            chunks = []
            while chunk := os.read(fd, 65536):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except InstallError:
        raise
    except OSError as exc:
        raise _fail("cannot read source file") from exc


def _validate_source_root(source_root: Path, uid: int) -> Path:
    if not source_root.is_absolute():
        raise _fail("source root is not absolute")
    try:
        canonical = source_root.resolve(strict=True)
        info = os.lstat(source_root)
    except OSError as exc:
        raise _fail("cannot inspect source root") from exc
    if canonical != source_root or stat.S_ISLNK(info.st_mode):
        raise _fail("source root is not canonical")
    _check_directory(info, uid, "source root")
    return canonical


def _fixed_runtime_bootstrap_assets(source_root: Path, uid: int) -> tuple[bytes, bytes, bytes]:
    helper = _trusted_source_file(source_root, "bin/manage-private-config.php", uid)
    manifest = _trusted_source_file(source_root, "fixed-runtime/legacy-manifest.json", uid)
    generation_manifest = _trusted_source_file(
        source_root, "fixed-runtime/generation-b9fd468-manifest.json", uid)
    if (len(generation_manifest) != _FIXED_GENERATION_MANIFEST_SIZE
            or hashlib.sha256(generation_manifest).hexdigest()
            != _FIXED_GENERATION_MANIFEST_SHA256):
        raise _fail("fixed generation manifest mismatch")
    return helper, manifest, generation_manifest


def build_bundle(source_root: Path, build_root: Path, runtime_record: RuntimeRecord) -> Path:
    uid = os.getuid()
    source_root = _validate_source_root(Path(source_root), uid)
    build_root = Path(build_root)
    if not build_root.is_absolute() or build_root.exists() or source_root in build_root.parents or build_root == source_root or Path("/Applications") in (build_root, *build_root.parents):
        raise _fail("unsafe build root")
    source_data = {name: _trusted_source_file(source_root, name, uid) for name in ("macos/AppLauncher.applescript", *_SOURCE_FILES)}
    fixed_helper, fixed_manifest, fixed_generation_manifest = _fixed_runtime_bootstrap_assets(
        source_root, uid)
    created_build_identity: tuple[int, int] | None = None
    try:
        build_root.mkdir(mode=0o700)
        if stat.S_IMODE(os.lstat(build_root).st_mode) != 0o700 or os.lstat(build_root).st_uid != uid:
            raise _fail("unsafe build root")
        build_info = os.lstat(build_root)
        created_build_identity = (build_info.st_dev, build_info.st_ino)
        _check_directory(build_info, uid, "build root", exact=0o700)
        build_fd = os.open(build_root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if _identity(build_info) != _identity(os.fstat(build_fd)):
                raise _fail("changed build root")
        finally:
            os.close(build_fd)
        bundle = build_root / "Xserverメール通知管理.app"
        subprocess.run(["/usr/bin/osacompile", "-o", str(bundle), "-"], input=source_data["macos/AppLauncher.applescript"], check=True, shell=False, capture_output=True)
        compiler_entries = _verify_compiler_bundle(bundle, uid)
        # Current osacompile versions add optional signing/icon artifacts.  This
        # app intentionally ships the plan's version-independent exact layout.
        signature = bundle / "Contents" / "_CodeSignature"
        if signature.exists():
            shutil.rmtree(signature)
        for optional in ("Assets.car", "applet.icns"):
            path = bundle / "Contents" / "Resources" / optional
            if path.exists():
                path.unlink()
        resources = bundle / "Contents" / "Resources"
        _verify_private_directory_chain(bundle, uid)
        (resources / "manager").mkdir(mode=0o700)
        (resources / "fixed-runtime").mkdir(mode=0o700)
        for source_name, destination_name in _SOURCE_FILES.items():
            destination = bundle / destination_name
            parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
            try:
                _write_private_file(parent_fd, destination.name, source_data[source_name], 0o700 if destination_name.endswith("launcher.sh") else 0o600)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        resources_fd = os.open(resources, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _write_private_file(resources_fd, "python-path", (runtime_record.path + "\n").encode())
            _write_private_file(resources_fd, "python-runtime.json", (json.dumps(asdict(runtime_record), sort_keys=True, separators=(",", ":")) + "\n").encode())
            os.fsync(resources_fd)
        finally:
            os.close(resources_fd)
        fixed_fd = os.open(resources / "fixed-runtime", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _write_private_file(fixed_fd, "manage-private-config.php", fixed_helper)
            _write_private_file(fixed_fd, "legacy-manifest.json", fixed_manifest)
            _write_private_file(fixed_fd, "generation-b9fd468-manifest.json",
                                fixed_generation_manifest)
            os.fsync(fixed_fd)
        finally:
            os.close(fixed_fd)
        _update_info_plist_nofollow(bundle, compiler_entries["Contents/Info.plist"])
        subprocess.run([
            "/usr/bin/codesign", "--force", "--deep", "--sign", "-",
            "--identifier", BUNDLE_IDENTIFIER, str(bundle),
        ], check=True, shell=False, capture_output=True)
        _require_exact_bundle_entries(bundle, uid)
        _set_exact_bundle_modes(bundle, uid)
        subprocess.run([
            "/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle),
        ], check=True, shell=False, capture_output=True)
        validate_bundle(bundle, source_root, uid)
        return bundle
    except Exception as exc:
        if created_build_identity is not None:
            try:
                current = os.lstat(build_root)
                if (current.st_dev, current.st_ino) == created_build_identity and stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(current.st_mode):
                    shutil.rmtree(build_root)
            except OSError:
                pass
        if isinstance(exc, InstallError):
            raise
        raise _fail("bundle build failed") from exc


def relative_bundle_files(bundle: Path) -> set[str]:
    result = set()
    for directory, dirs, files in os.walk(bundle, followlinks=False):
        for name in files:
            result.add((Path(directory) / name).relative_to(bundle).as_posix())
    return result


def _verify_compiler_bundle(bundle: Path, uid: int) -> dict[str, tuple[int, int, int, int, int]]:
    """Validate the complete compiler output before touching any path in it."""
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    seen_dirs: set[str] = set()
    seen_files: set[str] = set()
    try:
        for directory, dirs, files in os.walk(bundle, topdown=True, followlinks=False):
            base = Path(directory)
            relative_dir = "." if base == bundle else base.relative_to(bundle).as_posix()
            info = os.lstat(base)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != uid:
                raise _fail("unsafe compiler entry")
            seen_dirs.add(relative_dir)
            identities[relative_dir] = _identity(info)
            for name in (*dirs, *files):
                path = base / name
                relative = path.relative_to(bundle).as_posix()
                entry = os.lstat(path)
                if stat.S_ISLNK(entry.st_mode):
                    raise _fail("unsafe compiler entry")
                if name in files:
                    if not stat.S_ISREG(entry.st_mode) or entry.st_uid != uid:
                        raise _fail("unsafe compiler entry")
                    seen_files.add(relative)
                    identities[relative] = _identity(entry)
        if not _COMPILER_REQUIRED_DIRS.issubset(seen_dirs) or not seen_dirs <= _COMPILER_REQUIRED_DIRS | _COMPILER_OPTIONAL_DIRS:
            raise _fail("unexpected compiler layout")
        if not _COMPILER_REQUIRED_FILES.issubset(seen_files) or not seen_files <= _COMPILER_REQUIRED_FILES | _COMPILER_OPTIONAL_FILES:
            raise _fail("unexpected compiler layout")
        if ("Contents/_CodeSignature" in seen_dirs) != ("Contents/_CodeSignature/CodeResources" in seen_files):
            raise _fail("incomplete compiler layout")
        return identities
    except InstallError:
        raise
    except OSError as exc:
        raise _fail("cannot inspect compiler output") from exc


def _update_info_plist_nofollow(bundle: Path, expected_identity: tuple[int, int, int, int, int]) -> None:
    plist_path = bundle / "Contents/Info.plist"
    try:
        fd = os.open(plist_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if _identity(before) != expected_identity or not stat.S_ISREG(before.st_mode):
                raise _fail("changed compiler Info.plist")
            chunks = []
            while chunk := os.read(fd, 65536):
                chunks.append(chunk)
            plist = plistlib.loads(b"".join(chunks))
            plist["CFBundleIdentifier"] = BUNDLE_IDENTIFIER
            data = plistlib.dumps(plist, fmt=plistlib.FMT_BINARY, sort_keys=True)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            _write_all(fd, data)
            os.fsync(fd)
            if _identity(os.fstat(fd)) != expected_identity:
                raise _fail("changed compiler Info.plist")
        finally:
            os.close(fd)
    except InstallError:
        raise
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise _fail("cannot update compiler Info.plist") from exc


def _verify_private_directory_chain(bundle: Path, uid: int) -> None:
    for relative in (".", "Contents", "Contents/MacOS", "Contents/Resources", "Contents/Resources/Scripts"):
        path = bundle if relative == "." else bundle / relative
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise _fail("unsafe compiler directory")


def _set_exact_bundle_modes(bundle: Path, uid: int) -> None:
    entries = [(".", True)] + [(p, True) for p in sorted(_ALLOWED_DIRS - {"."})] + [(p, False) for p in sorted(EXPECTED_BUNDLE_FILES)]
    root_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        for relative, is_dir in entries:
            if relative == ".":
                os.fchmod(root_fd, 0o700)
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | (os.O_DIRECTORY if is_dir else 0)
            fd = os.open(relative, flags, dir_fd=root_fd)
            try:
                info = os.fstat(fd)
                if info.st_uid != uid or (not stat.S_ISDIR(info.st_mode) if is_dir else not stat.S_ISREG(info.st_mode)):
                    raise _fail("unsafe bundle entry")
                mode = 0o700 if is_dir or relative in _EXECUTABLE_BUNDLE_FILES else 0o600
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _require_exact_bundle_entries(bundle: Path, uid: int) -> None:
    found_dirs: set[str] = set()
    found_files: set[str] = set()
    for directory, dirs, files in os.walk(bundle, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_dir = "." if directory_path == bundle else directory_path.relative_to(bundle).as_posix()
        found_dirs.add(relative_dir)
        for name in dirs + files:
            entry = os.lstat(directory_path / name)
            if stat.S_ISLNK(entry.st_mode) or entry.st_uid != uid:
                raise _fail("unsafe bundle entry")
        for name in files:
            path = directory_path / name
            found_files.add(path.relative_to(bundle).as_posix())
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise _fail("unsafe bundle entry")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if _identity(info) != _identity(os.fstat(fd)):
                    raise _fail("changed bundle entry")
            finally:
                os.close(fd)
    if found_dirs != _ALLOWED_DIRS or found_files != EXPECTED_BUNDLE_FILES:
        raise _fail("unexpected bundle layout")


def _read_regular_nofollow(path: Path) -> bytes:
    before = os.lstat(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        after = os.fstat(fd)
        if _identity(before) != _identity(after) or not stat.S_ISREG(after.st_mode):
            raise _fail("changed bundle entry")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


_WEBHOOK = re.compile(rb"https://webhook[.]worksmobile[.]com/message/[^\s\"'<>]{20,}", re.I)
_EMAIL = re.compile(rb"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9.-]+[.][A-Za-z]{2,})")
_HOST = re.compile(rb"\b(?:sv\d{3,}|xs\d{3,}|[a-z0-9-]+[.]xserver[.]jp)\b", re.I)
_API_KEY = re.compile(rb"(?:XSERVER[_ -]?API[_ -]?KEY\s*[=:]\s*)?[A-Fa-f0-9]{32,}", re.I)
_XSERVER_API_KEY = re.compile(rb"(?<![A-Za-z0-9_])xs_[A-Za-z0-9._~-]{12,}(?![A-Za-z0-9_])")
_SECRET_NAME = re.compile(rb"(?:^|[/\\])[^/\\\s]*secret[^/\\\s]*[.](?:json|ya?ml|ini|env)(?:\b|$)", re.I)


def _contains_secret(data: bytes, forbidden_tokens: tuple[bytes, ...] = ()) -> bool:
    if any(token and token in data for token in forbidden_tokens):
        return True
    if _WEBHOOK.search(data) or _HOST.search(data) or _API_KEY.search(data) or _XSERVER_API_KEY.search(data) or _SECRET_NAME.search(data):
        return True
    return any(match.group(1).lower() != b"example.invalid" for match in _EMAIL.finditer(data))


def validate_bundle(bundle: Path, source_root: Path, uid: int, *, forbidden_tokens: tuple[bytes, ...] = ()) -> None:
    bundle = Path(bundle)
    if type(forbidden_tokens) is not tuple or any(type(token) is not bytes or not token for token in forbidden_tokens):
        raise _fail("invalid secret denylist")
    _validate_source_root(Path(source_root), uid)
    _require_exact_bundle_entries(bundle, uid)
    found_dirs: set[str] = set()
    found_files: set[str] = set()
    for directory, dirs, files in os.walk(bundle, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_dir = "." if directory_path == bundle else directory_path.relative_to(bundle).as_posix()
        found_dirs.add(relative_dir)
        info = os.lstat(directory_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise _fail("unsafe bundle entry")
        for name in dirs + files:
            path = directory_path / name
            entry = os.lstat(path)
            if stat.S_ISLNK(entry.st_mode):
                raise _fail("unsafe bundle entry")
        for name in files:
            path = directory_path / name
            relative = path.relative_to(bundle).as_posix()
            found_files.add(relative)
            info = os.lstat(path)
            mode = stat.S_IMODE(info.st_mode)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or mode & 0o022:
                raise _fail("unsafe bundle entry")
            if relative in _EXECUTABLE_BUNDLE_FILES:
                if not mode & 0o100:
                    raise _fail("missing executable mode")
            elif mode & 0o111:
                raise _fail("unexpected executable mode")
            contents = _read_regular_nofollow(path)
            if (relative in {
                    "Contents/Resources/fixed-runtime/legacy-manifest.json",
                    "Contents/Resources/fixed-runtime/generation-b9fd468-manifest.json",
                    }
                    and any(token and token in contents for token in forbidden_tokens)):
                raise _fail("bundle contains sensitive data")
            if (relative not in {
                    "Contents/Resources/fixed-runtime/legacy-manifest.json",
                    "Contents/Resources/fixed-runtime/generation-b9fd468-manifest.json",
                    }
                    and _contains_secret(contents, forbidden_tokens)):
                raise _fail("bundle contains sensitive data")
    if found_dirs != _ALLOWED_DIRS or found_files != EXPECTED_BUNDLE_FILES:
        raise _fail("unexpected bundle layout")
    try:
        expected_helper, expected_manifest, expected_generation_manifest = \
            _fixed_runtime_bootstrap_assets(source_root, uid)
        if (_read_regular_nofollow(bundle / "Contents/Resources/fixed-runtime/manage-private-config.php")
                != expected_helper
                or _read_regular_nofollow(bundle / "Contents/Resources/fixed-runtime/legacy-manifest.json")
                != expected_manifest
                or _read_regular_nofollow(bundle / "Contents/Resources/fixed-runtime/generation-b9fd468-manifest.json")
                != expected_generation_manifest):
            raise _fail("fixed runtime bootstrap assets mismatch")
        subprocess.run(["/usr/bin/plutil", "-lint", str(bundle / "Contents/Info.plist")], check=True, shell=False, capture_output=True)
        plist = plistlib.loads(_read_regular_nofollow(bundle / "Contents/Info.plist"))
        if plist.get("CFBundleIdentifier") != BUNDLE_IDENTIFIER:
            raise _fail("invalid bundle identifier")
        runtime_data = json.loads(_read_regular_nofollow(bundle / "Contents/Resources/python-runtime.json").decode("utf-8"), object_pairs_hook=_strict_pairs)
        fields = {"path", "device", "inode", "major", "minor"}
        if type(runtime_data) is not dict or set(runtime_data) != fields or type(runtime_data["path"]) is not str or any(type(runtime_data[key]) is not int for key in fields - {"path"}):
            raise _fail("invalid runtime metadata")
        record = RuntimeRecord(**runtime_data)
        if not os.path.isabs(record.path) or record.major != 3 or record.minor not in {13, 14}:
            raise _fail("invalid runtime metadata")
        try:
            current_record = record_python_runtime(record.path, (record.major, record.minor))
        except RuntimeError as exc:
            raise _fail("invalid runtime metadata") from exc
        if current_record != record:
            raise _fail("runtime identity mismatch")
        if _read_regular_nofollow(bundle / "Contents/Resources/python-path").decode("utf-8") != record.path + "\n":
            raise _fail("runtime metadata mismatch")
    except InstallError:
        raise
    except Exception as exc:
        raise _fail("invalid bundle metadata") from exc


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _check_bundle_identifier(bundle: Path) -> None:
    try:
        value = plistlib.loads(_read_regular_nofollow(bundle / "Contents/Info.plist"))
    except Exception as exc:
        raise _fail("invalid bundle metadata") from exc
    if value.get("CFBundleIdentifier") != BUNDLE_IDENTIFIER:
        raise _fail("invalid bundle identifier")


def _validate_existing_destination(
    destination: Path,
    uid: int,
):
    """Pin a current or explicitly known legacy app tree before exchange."""
    _check_bundle_identifier(destination)
    present_files = frozenset(relative_bundle_files(destination))
    matching_layouts = [
        layout for layout in _EXISTING_DESTINATION_FILE_LAYOUTS
        if present_files == layout
    ]
    if len(matching_layouts) != 1:
        raise _fail("unsupported existing bundle layout")
    allowed_dirs = set(_ALLOWED_DIRS)
    if not any(path.startswith("Contents/Resources/fixed-runtime/")
               for path in matching_layouts[0]):
        allowed_dirs.remove("Contents/Resources/fixed-runtime")
    return create_bundle_manifest(destination, uid,
                                  allowed_files=set(matching_layouts[0]),
                                  allowed_dirs=allowed_dirs)


def install_bundle(bundle: Path, destination: Path, uid: int, *, txn_id: str | None = None) -> InstallOutcome:
    """Copy and atomically publish the fixed per-user application bundle.

    The source is pinned by an immutable manifest before any destination
    mutation.  A durable PREPARED record precedes the exclusive rename/swap;
    the transaction tree is removed only through an identity-bound receipt.
    """
    expected = Path.home() / "Applications" / "Xserverメール通知管理.app"
    if destination != expected:
        raise _fail("invalid installation destination")
    recover_pending_transactions(uid)
    lease = _prepare_common_transaction(Path(bundle), uid, txn_id or secrets.token_hex(16))
    try:
        outcome = lease.publish()
        lease.mark_committing()
        lease.complete()
        return outcome
    except Exception:
        lease.abort()
        raise
    finally:
        lease.close()


class _CommonInstallLease:
    """Own the installer lock from durable intent through config+app commit."""
    def __init__(self, *, applications_fd: int, installer_fd: int, transaction_fd: int,
                 lock_fd: int, txn_id: str, manifest, destination: Path,
                 old_manifest, updated: bool):
        self.applications_fd = applications_fd
        self.installer_fd = installer_fd
        self.transaction_fd = transaction_fd
        self.lock_fd = lock_fd
        self.txn_id = txn_id
        self.manifest = manifest
        self.destination = destination
        self.old_manifest = old_manifest
        self.updated = updated
        self.published = False
        self.finished = False
        self.config_marker = None

    def _details(self) -> dict:
        details = {"destination": self.destination.name, "staged": "staged.app",
                   "tree_sha256": self.manifest.tree_sha256,
                   "old_tree_sha256": self.old_manifest.tree_sha256 if self.old_manifest else None}
        if self.config_marker is not None:
            details["config"] = self.config_marker
        return details

    def publish(self) -> InstallOutcome:
        journal = Journal(self.transaction_fd, self.txn_id)
        journal.append("PREPARED" if self.updated else "PREPARED_NEW", self._details())
        if self.updated:
            rename_swap(self.transaction_fd, b"staged.app", self.applications_fd,
                        os.fsencode(self.destination.name))
        else:
            rename_exclusive(self.transaction_fd, b"staged.app", self.applications_fd,
                             os.fsencode(self.destination.name))
        os.fsync(self.applications_fd)
        self.published = True
        _check_bundle_identifier(self.destination)
        installed = create_bundle_manifest(self.destination, os.getuid(),
                                           allowed_files=set(EXPECTED_BUNDLE_FILES),
                                           allowed_dirs=set(_ALLOWED_DIRS))
        if installed.tree_sha256 != self.manifest.tree_sha256:
            raise _fail("installed bundle verification failed")
        journal.append("VERIFIED", self._details())
        return InstallOutcome.UPDATED if self.updated else InstallOutcome.NEW_INSTALLED

    def record_config_prepared(self, marker: dict) -> None:
        """Bind the exact config names/hashes into the common app journal."""
        allowed = {"schema", "txn_id", "phase", "temp", "backup", "trash",
                   "old_config", "old_sha256", "new_sha256"}
        if set(marker) != allowed or marker.get("txn_id") != self.txn_id:
            raise _fail("invalid shared configuration marker")
        self.config_marker = dict(marker)
        Journal(self.transaction_fd, self.txn_id).append("CONFIG_PREPARED", self._details())

    def abort(self) -> None:
        if self.finished:
            return
        if self.published:
            if self.updated:
                rename_swap(self.transaction_fd, b"staged.app", self.applications_fd,
                            os.fsencode(self.destination.name))
            else:
                rename_exclusive(self.applications_fd, os.fsencode(self.destination.name),
                                 self.transaction_fd, b"staged.app")
            os.fsync(self.applications_fd)
        Journal(self.transaction_fd, self.txn_id).append("ABORTED", self._details())
        self._cleanup()

    def complete(self) -> None:
        if self.finished:
            raise _fail("transaction already finished")
        config_hash = self.config_marker["new_sha256"] if self.config_marker else "absent"
        _assert_completion_state(self.applications_fd, os.getuid(),
                                 self.manifest.tree_sha256, config_hash)
        journal = Journal(self.transaction_fd, self.txn_id)
        journal.append("CLEANED", self._details())
        write_completion_receipt(self.installer_fd, self.txn_id,
                                 self.manifest.tree_sha256, config_hash)
        self._cleanup()

    def mark_committing(self) -> None:
        Journal(self.transaction_fd, self.txn_id).append("COMMITTING", self._details())

    def _cleanup(self) -> None:
        expected_root, expected_entries = capture_cleanup_inventory(self.transaction_fd)
        receipt = create_cleanup_receipt(self.installer_fd, f"transaction.{self.txn_id}", self.txn_id,
                                         expected_root, expected_entries)
        resume_cleanup(self.installer_fd, receipt)
        self.finished = True

    def close(self) -> None:
        for name in ("transaction_fd", "lock_fd", "installer_fd", "applications_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)


def _prepare_common_transaction(bundle: Path, uid: int, txn_id: str) -> _CommonInstallLease:
    """Durably journal and stage the app before configuration can be renamed."""
    ensure_unprivileged()
    if not re.fullmatch(r"[0-9a-f]{32}", txn_id):
        raise _fail("invalid transaction id")
    destination = Path.home() / "Applications/Xserverメール通知管理.app"
    manifest = create_bundle_manifest(Path(bundle), uid, allowed_files=set(EXPECTED_BUNDLE_FILES),
                                      allowed_dirs=set(_ALLOWED_DIRS))
    home_fd = _open_dir(Path.home(), uid, "home directory")
    applications_fd = installer_fd = transaction_fd = lock_fd = -1
    txn_name: str | None = None
    staged_identity: tuple[int, int, int, int, int] | None = None
    journal_prepared = False
    try:
        try:
            os.mkdir("Applications", 0o700, dir_fd=home_fd); os.fsync(home_fd)
        except FileExistsError:
            pass
        applications_fd = _open_dir("Applications", uid, "Applications directory", dir_fd=home_fd)
        if os.fstat(applications_fd).st_dev != manifest.source_root.dev:
            raise _fail("mount boundary")
        try:
            os.mkdir(".xserver-mail-lineworks-installer", 0o700, dir_fd=applications_fd); os.fsync(applications_fd)
        except FileExistsError:
            pass
        installer_fd = _open_dir(".xserver-mail-lineworks-installer", uid, "installer directory",
                                 dir_fd=applications_fd, exact=0o700)
        lock_fd = os.open("lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
                          dir_fd=installer_fd)
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != uid or
                lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) != 0o600):
            raise _fail("unsafe installer lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _fail("another installation is running") from exc
        recover_transactions(applications_fd, uid, _lock_held=True)
        for attempt in range(16):
            candidate = txn_id if attempt == 0 else secrets.token_hex(16)
            txn_name = f"transaction.{candidate}"
            try:
                os.mkdir(txn_name, 0o700, dir_fd=installer_fd)
                txn_id = candidate
                break
            except FileExistsError:
                continue
        else:
            raise _fail("transaction id collision")
        os.fsync(installer_fd)
        transaction_fd = _open_dir(txn_name, uid, "transaction directory", dir_fd=installer_fd, exact=0o700)
        os.mkdir("staged.app", 0o700, dir_fd=transaction_fd)
        stage_fd = _open_dir("staged.app", uid, "staged application", dir_fd=transaction_fd, exact=0o700)
        try:
            staged_identity = _identity(os.fstat(stage_fd))
            copy_manifest_tree(manifest, bundle, stage_fd)
        finally:
            os.close(stage_fd)
        updated = destination.name in os.listdir(applications_fd)
        old_manifest = _validate_existing_destination(destination, uid) if updated else None
        details = {"destination": destination.name, "staged": "staged.app",
                   "tree_sha256": manifest.tree_sha256,
                   "old_tree_sha256": old_manifest.tree_sha256 if old_manifest else None}
        Journal(transaction_fd, txn_id).append("TXN_PREPARED", details)
        journal_prepared = True
        os.close(home_fd)
        return _CommonInstallLease(applications_fd=applications_fd, installer_fd=installer_fd,
                                   transaction_fd=transaction_fd, lock_fd=lock_fd, txn_id=txn_id,
                                   manifest=manifest, destination=destination,
                                   old_manifest=old_manifest, updated=updated)
    except Exception:
        if (not journal_prepared and transaction_fd >= 0 and installer_fd >= 0 and
                txn_name is not None and staged_identity is not None):
            try:
                if set(os.listdir(transaction_fd)) != {"staged.app"}:
                    raise _fail("unknown pre-journal transaction content")
                current_stage_fd = _open_dir("staged.app", uid, "staged application",
                                             dir_fd=transaction_fd, exact=0o700)
                try:
                    if _identity(os.fstat(current_stage_fd)) != staged_identity:
                        raise _fail("changed staged application")
                    expected_dirs = {item.path: item.identity.mode for item in manifest.directories}
                    expected_files = {item.path: (item.identity.mode, item.size, item.sha256)
                                      for item in manifest.files}
                    actual_dirs: dict[str, int] = {}
                    actual_files: dict[str, tuple[int, int, str]] = {}
                    def scan(fd: int, prefix: str = "") -> None:
                        actual_dirs[prefix or "."] = stat.S_IMODE(os.fstat(fd).st_mode)
                        for name in os.listdir(fd):
                            relative = f"{prefix}/{name}" if prefix else name
                            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY |
                                                getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
                                try:
                                    scan(child, relative)
                                finally:
                                    os.close(child)
                            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                                child = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                                dir_fd=fd)
                                try:
                                    body = b""
                                    while chunk := os.read(child, 65536):
                                        body += chunk
                                finally:
                                    os.close(child)
                                actual_files[relative] = (stat.S_IMODE(info.st_mode), len(body),
                                                          hashlib.sha256(body).hexdigest())
                            else:
                                raise _fail("changed staged application")
                    scan(current_stage_fd)
                    if actual_dirs != expected_dirs or actual_files != expected_files:
                        raise _fail("changed staged application")
                finally:
                    os.close(current_stage_fd)
                expected_root, expected_entries = capture_cleanup_inventory(transaction_fd)
                receipt = create_cleanup_receipt(installer_fd, txn_name, txn_id,
                                                 expected_root, expected_entries)
                resume_cleanup(installer_fd, receipt)
            except Exception:
                # Unknown or replaced content must remain quarantined for inspection.
                pass
        for fd in (transaction_fd, lock_fd, installer_fd, applications_fd, home_fd):
            if fd >= 0:
                os.close(fd)
        raise


def install_with_config(source_root: Path, build_root: Path, config_values: object, uid: int) -> InstallOutcome:
    """Build, stage configuration, publish the app, then commit configuration."""
    recover_pending_transactions(uid)
    runtime = validate_installer_python(tuple(sys.version_info[:2]), sys.executable)
    bundle = build_bundle(source_root, build_root, runtime)
    config_path = Path.home() / "Library/Application Support/XserverMailLineworks/config.json"
    txn_id = secrets.token_hex(16)
    lease = _prepare_common_transaction(bundle, uid, txn_id)
    transaction = None
    try:
        transaction = write_config_atomic(config_path, config_values, uid, txn_id=lease.txn_id,
                                          before_publish=lease.record_config_prepared)
        outcome = lease.publish()
    except Exception:
        if transaction is not None:
            transaction.rollback()
        lease.abort()
        lease.close()
        raise
    try:
        lease.mark_committing()
        transaction.commit()
        lease.complete()
        return outcome
    finally:
        lease.close()


def recover_pending_transactions(uid: int) -> int:
    """Fail closed or converge every durable per-user transaction before writes."""
    applications = Path.home() / "Applications"
    installer = applications / ".xserver-mail-lineworks-installer"
    if not installer.exists():
        return 0
    app_fd = _open_dir(applications, uid, "Applications directory")
    try:
        return recover_transactions(app_fd, uid)
    finally:
        os.close(app_fd)


def _recover_config_marker(txn_id: str, uid: int, *, app_state: str, expected_marker=None, _fault=None) -> None:
    """Converge the config half of a shared transaction, idempotently."""
    directory = Path.home() / "Library/Application Support/XserverMailLineworks"
    try:
        os.lstat(directory)
    except FileNotFoundError:
        if expected_marker is not None:
            raise _fail("transactionを隔離して再実行してください")
        return
    fd = _open_dir(directory, uid, "configuration directory", exact=0o700)
    marker_name = f".transaction-{txn_id}.json"
    try:
        try:
            marker_fd = os.open(marker_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
        except FileNotFoundError:
            if expected_marker is not None:
                try:
                    committed_fd = os.open("config.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
                    try:
                        committed = os.fstat(committed_fd)
                        body = b""
                        while chunk := os.read(committed_fd, 65536): body += chunk
                    finally:
                        os.close(committed_fd)
                    if (not stat.S_ISREG(committed.st_mode) or committed.st_uid != uid or
                            stat.S_IMODE(committed.st_mode) != 0o600 or
                            __import__("hashlib").sha256(body).hexdigest() != expected_marker.get("new_sha256") or
                            any(name in os.listdir(fd) for name in filter(None, (
                                expected_marker.get("temp"), expected_marker.get("backup"), expected_marker.get("trash"))))):
                        raise _fail("transactionを隔離して再実行してください")
                except (OSError, AttributeError) as exc:
                    raise _fail("transactionを隔離して再実行してください") from exc
            return
        try:
            marker_info = os.fstat(marker_fd)
            if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != uid or stat.S_IMODE(marker_info.st_mode) != 0o600:
                raise _fail("transactionを隔離して再実行してください")
            marker_body = b""
            while chunk := os.read(marker_fd, 65536):
                marker_body += chunk
        finally:
            os.close(marker_fd)
        try:
            marker = json.loads(marker_body, object_pairs_hook=_strict_pairs)
            valid = (set(marker) == {"schema", "txn_id", "phase", "temp", "backup", "trash", "old_config",
                                    "old_sha256", "new_sha256"} and marker["schema"] == 1 and
                     marker["txn_id"] == txn_id and marker["phase"] == "TXN_PREPARED" and
                     isinstance(marker["new_sha256"], str) and
                     (marker["backup"] is None or isinstance(marker["backup"], str)))
        except (ValueError, TypeError, json.JSONDecodeError):
            valid = False
        if not valid:
            raise _fail("transactionを隔離して再実行してください")
        if expected_marker is not None and marker != expected_marker:
            raise _fail("transactionを隔離して再実行してください")
        expected_temp = f".config-{txn_id}"
        safe_backup = marker["backup"] is None or bool(re.fullmatch(r"[.]backup-[0-9a-f]{32}", marker["backup"]))
        if (marker["temp"] != expected_temp or marker["trash"] != f".trash-{txn_id}" or not safe_backup or
                type(marker["old_config"]) is not bool or
                marker["old_config"] != (marker["backup"] is not None) or
                not re.fullmatch(r"[0-9a-f]{64}", marker["new_sha256"]) or
                (marker["old_sha256"] is not None and
                 not re.fullmatch(r"[0-9a-f]{64}", marker["old_sha256"])) or
                marker["old_config"] != (marker["old_sha256"] is not None)):
            raise _fail("transactionを隔離して再実行してください")

        def file_hash(name: str) -> str | None:
            try:
                item = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(item)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600:
                    raise _fail("transactionを隔離して再実行してください")
                body = b""
                while chunk := os.read(item, 65536):
                    body += chunk
                return __import__("hashlib").sha256(body).hexdigest()
            finally:
                os.close(item)

        backup = marker["backup"]
        names = {"config": "config.json", "backup": backup, "temp": marker["temp"], "trash": marker["trash"]}
        expected_artifacts = {value for value in names.values() if value} | {marker_name}
        for entry in os.listdir(fd):
            if entry.startswith((".config-", ".backup-", ".trash-", ".transaction-")) and entry not in expected_artifacts:
                raise _fail("transactionを隔離して再実行してください")

        def role(name: str) -> str:
            actual = file_hash(names[name]) if names[name] else None
            if actual is None: return "absent"
            if actual == marker["new_sha256"]: return "new"
            if actual == marker["old_sha256"]: return "old"
            raise _fail("transactionを隔離して再実行してください")

        def mutate(action: str) -> None:
            mapping = {
                "OLD_TO_BACKUP": ("config", "backup"), "PUBLISH_TEMP": ("temp", "config"),
                "NEW_TO_TRASH": ("config", "trash"), "RESTORE_BACKUP": ("backup", "config"),
            }
            drops = {"DROP_TEMP": "temp", "DROP_BACKUP": "backup", "DROP_TRASH": "trash"}
            if action in mapping:
                source, target = mapping[action]
                rename_exclusive(fd, os.fsencode(names[source]), fd, os.fsencode(names[target]))
            else:
                os.unlink(names[drops[action]], dir_fd=fd)
            os.fsync(fd)
            if _fault:
                _fault(action)

        for _ in range(8):
            state = {key: role(key) for key in names}
            # Recovery-action intermediate states caused by a crash after mutation.
            if state == {"config": "absent", "backup": "old", "temp": "absent", "trash": "new"}:
                action = "RESTORE_BACKUP"
            elif state == {"config": "old", "backup": "absent", "temp": "absent", "trash": "new"}:
                action = "DROP_TRASH"
            elif state == {"config": "absent", "backup": "absent", "temp": "absent", "trash": "new"}:
                action = "DROP_TRASH"
            else:
                if marker["old_config"]:
                    if state["config"] == "old" and state["backup"] == "absent" and state["temp"] == "new" and state["trash"] == "absent": cell = "a"
                    elif state["config"] == "absent" and state["backup"] == "old" and state["temp"] == "new" and state["trash"] == "absent": cell = "b"
                    elif state["config"] == "new" and state["backup"] == "old" and state["temp"] == "absent" and state["trash"] == "absent": cell = "c"
                    elif state == {"config": "old", "backup": "absent", "temp": "absent", "trash": "absent"}: break
                    elif state == {"config": "new", "backup": "absent", "temp": "absent", "trash": "absent"}: break
                    else: raise _fail("transactionを隔離して再実行してください")
                else:
                    if state["config"] == "absent" and state["backup"] == "absent" and state["temp"] == "new" and state["trash"] == "absent": cell = "d"
                    elif state["config"] == "new" and state["backup"] == "absent" and state["temp"] == "absent" and state["trash"] == "absent": cell = "e"
                    elif state == {"config": "absent", "backup": "absent", "temp": "absent", "trash": "absent"}: break
                    else: raise _fail("transactionを隔離して再実行してください")
                actions = classify_config_state(cell, app_state).actions
                if not actions: break
                action = actions[0]
            mutate(action)
        else:
            raise _fail("transactionを隔離して再実行してください")
        os.unlink(marker_name, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def recover_transactions(applications_fd: int, uid: int, *, _lock_held: bool = False) -> int:
    """Resume durable app publication after a process crash.

    Recovery is deliberately identity/hash driven. A PREPARED transaction is
    either cleaned as unpublished or completed forward when the destination is
    already the recorded new tree. Ambiguous layouts are left untouched.
    """
    applications = os.fstat(applications_fd)
    if not stat.S_ISDIR(applications.st_mode) or applications.st_uid != uid or stat.S_IMODE(applications.st_mode) & 0o022:
        raise _fail("unsafe Applications directory")
    installer_fd = _open_dir(".xserver-mail-lineworks-installer", uid, "installer directory",
                             dir_fd=applications_fd, exact=0o700)
    recovery_lock_fd = -1
    recovered = 0
    try:
        if not _lock_held:
            recovery_lock_fd = os.open("lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                                       0o600, dir_fd=installer_fd)
            lock_info = os.fstat(recovery_lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != uid or
                    lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) != 0o600):
                raise _fail("unsafe installer lock")
            try:
                fcntl.flock(recovery_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise _fail("another installation is running") from exc
        entries = set(os.listdir(installer_fd))
        for name in entries:
            if name == "lock" or name == "completed" or re.fullmatch(r"transaction[.][0-9a-f]{32}", name) or re.fullmatch(r"cleanup[.][0-9a-f]{32}[.]json", name):
                continue
            raise _fail("transactionを隔離して再実行してください")
        for receipt in sorted(name for name in entries if name.startswith("cleanup.")):
            resume_cleanup(installer_fd, receipt)
            recovered += 1
        # The shared installer lock also serializes uninstall recovery.
        from .uninstall_app import _recover_uninstall_transactions
        recovered += _recover_uninstall_transactions(applications_fd, installer_fd, uid)
        for name in sorted(os.listdir(installer_fd)):
            if not name.startswith("transaction."):
                continue
            txn_id = name.removeprefix("transaction.")
            if len(txn_id) != 32 or any(c not in "0123456789abcdef" for c in txn_id):
                raise _fail("transactionを隔離して再実行してください")
            txn_fd = _open_dir(name, uid, "transaction directory", dir_fd=installer_fd, exact=0o700)
            cleanup = False
            config_app_state: str | None = None
            try:
                head = Journal(txn_fd, txn_id).recover_head()
                phase, details = head["phase"], head["details"]
                if phase in {"VERIFIED", "COMMITTING", "CLEANED"}:
                    cleanup = True
                    config_app_state = "new"
                elif phase in ("TXN_PREPARED", "CONFIG_PREPARED", "PREPARED", "PREPARED_NEW"):
                    expected_hash = details.get("tree_sha256")
                    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                        raise _fail("transactionを隔離して再実行してください")
                    names = set(os.listdir(applications_fd))
                    txn_names = set(os.listdir(txn_fd))
                    destination_present = "Xserverメール通知管理.app" in names
                    stage_present = "staged.app" in txn_names

                    def digest(parent_fd: int, child_name: str) -> str:
                        descriptor = os.open(child_name, os.O_RDONLY | os.O_DIRECTORY |
                                             getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        try:
                            return tree_digest_fd(descriptor)
                        finally:
                            os.close(descriptor)

                    destination_hash = digest(applications_fd, "Xserverメール通知管理.app") if destination_present else None
                    stage_hash = digest(txn_fd, "staged.app") if stage_present else None
                    if phase in {"TXN_PREPARED", "CONFIG_PREPARED"}:
                        expected_old = details.get("old_tree_sha256")
                        if not stage_present or stage_hash != expected_hash:
                            raise _fail("transactionを隔離して再実行してください")
                        if expected_old is None and not destination_present:
                            cleanup = True; config_app_state = "absent"
                        elif (isinstance(expected_old, str) and destination_present and
                              destination_hash == expected_old):
                            cleanup = True; config_app_state = "old"
                        else:
                            raise _fail("transactionを隔離して再実行してください")
                    elif phase == "PREPARED_NEW":
                        if destination_present and not stage_present and destination_hash == expected_hash:
                            Journal(txn_fd, txn_id).append("VERIFIED", details)
                            cleanup = True
                            config_app_state = "new"
                        elif not destination_present and stage_present and stage_hash == expected_hash:
                            cleanup = True
                            config_app_state = "absent"
                        else:
                            raise _fail("transactionを隔離して再実行してください")
                    else:
                        if not (destination_present and stage_present):
                            raise _fail("transactionを隔離して再実行してください")
                        if destination_hash == expected_hash and stage_hash != expected_hash:
                            Journal(txn_fd, txn_id).append("VERIFIED", details)
                            cleanup = True
                            config_app_state = "new"
                        elif stage_hash == expected_hash and destination_hash != expected_hash:
                            cleanup = True
                            config_app_state = "old"
                        else:
                            raise _fail("transactionを隔離して再実行してください")
                else:
                    raise _fail("transactionを隔離して再実行してください")
            finally:
                os.close(txn_fd)
            if cleanup:
                if config_app_state is not None:
                    expected_config = details.get("config")
                    _recover_config_marker(txn_id, uid, app_state=config_app_state,
                                           expected_marker=expected_config)
                if config_app_state == "new":
                    config_hash = expected_config["new_sha256"] if expected_config is not None else "absent"
                    _assert_completion_state(applications_fd, uid, details["tree_sha256"], config_hash)
                    write_completion_receipt(installer_fd, txn_id, details["tree_sha256"], config_hash)
                cleanup_fd = _open_dir(name, uid, "cleanup transaction", dir_fd=installer_fd,
                                       exact=0o700)
                try:
                    expected_root, expected_entries = capture_cleanup_inventory(cleanup_fd)
                finally:
                    os.close(cleanup_fd)
                receipt = create_cleanup_receipt(installer_fd, name, txn_id,
                                                 expected_root, expected_entries)
                resume_cleanup(installer_fd, receipt)
                recovered += 1
        return recovered
    finally:
        if recovery_lock_fd >= 0:
            os.close(recovery_lock_fd)
        os.close(installer_fd)


def _current_completion_hashes(applications_fd: int, uid: int) -> tuple[str, str]:
    applications_path = Path.home() / "Applications"
    expected_parent = _identity(os.fstat(applications_fd))
    if _identity(os.lstat(applications_path)) != expected_parent:
        raise _fail("Applications directory changed")
    app_hash = create_bundle_manifest(
        applications_path / "Xserverメール通知管理.app", uid,
        allowed_files=set(EXPECTED_BUNDLE_FILES), allowed_dirs=set(_ALLOWED_DIRS),
    ).tree_sha256
    if _identity(os.lstat(applications_path)) != expected_parent:
        raise _fail("Applications directory changed")
    config_path = Path.home() / "Library/Application Support/XserverMailLineworks/config.json"
    try:
        _, config_hash = load_config_with_digest(config_path, uid)
    except FileNotFoundError:
        config_hash = "absent"
    return app_hash, config_hash


def _assert_completion_state(applications_fd: int, uid: int, expected_app: str,
                             expected_config: str) -> tuple[str, str]:
    try:
        actual = _current_completion_hashes(applications_fd, uid)
    except InstallError:
        raise
    except Exception as exc:
        raise _fail("completion state invalid") from exc
    if actual != (expected_app, expected_config):
        raise _fail("completion state changed")
    return actual


def verify_installation_completion(uid: int) -> dict:
    """Recompute installed app/config hashes and validate the latest receipt."""
    applications_path = Path.home() / "Applications"
    applications_fd = _open_dir(applications_path, uid, "Applications directory")
    installer_fd = lock_fd = -1
    try:
        installer_fd = _open_dir(".xserver-mail-lineworks-installer", uid,
                                 "installer directory", dir_fd=applications_fd, exact=0o700)
        lock_fd = os.open("lock", os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=installer_fd)
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != uid
                or lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) != 0o600):
            raise _fail("unsafe installer lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _fail("another installation is running") from exc
        app_hash, config_hash = _current_completion_hashes(applications_fd, uid)
        result = verify_latest_completion_receipt(installer_fd, app_hash, config_hash)
        if set(os.listdir(installer_fd)) != {"lock", "completed"}:
            raise _fail("completion state changed")
        return result
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if installer_fd >= 0:
            os.close(installer_fd)
        os.close(applications_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--verify-completion-receipt", action="store_true")
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args(argv)
    try:
        ensure_unprivileged()
        if args.verify_completion_receipt:
            if not args.latest or args.source_root is not None:
                raise _fail("invalid completion verification arguments")
            verify_installation_completion(os.getuid())
            print("インストール完了レシートを確認しました。")
            return 0
        if args.source_root is None or args.latest:
            raise _fail("source root is required")
        source_root = _validate_source_root(args.source_root, os.getuid())
        prompts = {
            "servername": "Xserver初期ドメイン: ", "command_path": "PHPスクリプトのサーバー絶対パス: ",
            "ftps_host": "FTPSホスト: ", "config_path": "FTPS基準の秘密設定パス: ", "filesystem_home": "Xserverホームディレクトリ: ",
            "ssh_alias": "専用SSH設定（~/.ssh/xserver-mail-lineworks.conf）の接続alias: ",
        }
        values = validate_config({key: input(prompt) for key, prompt in prompts.items()})
        build_root = Path(tempfile.mkdtemp(prefix="xserver-mail-lineworks-", dir=str(source_root.parent)))
        os.chmod(build_root, 0o700)
        # build_bundle requires ownership of creating its new directory.
        build_root.rmdir()
        config_path = Path.home() / "Library" / "Application Support" / "XserverMailLineworks" / "config.json"
        if config_path.exists() and input("既存設定を一時的に更新する場合は「上書きします」と入力: ") != "上書きします":
            print("既存設定は変更しませんでした。")
            return 1
        outcome = install_with_config(source_root, build_root, values, os.getuid())
        print(f"インストール完了: {outcome.value}")
        return 0
    except Exception:
        print("インストーラーの安全性検証に失敗しました。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
