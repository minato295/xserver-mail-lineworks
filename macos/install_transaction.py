"""Fail-closed, unprivileged macOS application install transactions."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import re
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

RENAME_SWAP = 0x00000002
RENAME_EXCL = 0x00000004
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class InstallError(RuntimeError):
    pass


class RecoveryError(InstallError):
    pass


@dataclass(frozen=True)
class FileIdentity:
    dev: int
    ino: int
    uid: int
    mode: int


@dataclass(frozen=True)
class ManifestDirectory:
    path: str
    identity: FileIdentity


@dataclass(frozen=True)
class ManifestFile:
    path: str
    identity: FileIdentity
    size: int
    sha256: str


@dataclass(frozen=True)
class BundleManifest:
    source_root: FileIdentity
    directories: tuple[ManifestDirectory, ...]
    files: tuple[ManifestFile, ...]
    tree_sha256: str


def _file_identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))


def create_bundle_manifest(bundle: Path, uid: int, *, allowed_files: set[str], allowed_dirs: set[str]) -> BundleManifest:
    bundle = Path(bundle)
    root_info = os.lstat(bundle)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode) or root_info.st_uid != uid:
        raise InstallError("unsafe bundle root")
    dirs, files, names = [], [], set()
    for current, dirnames, filenames in os.walk(bundle, followlinks=False):
        relative_dir = os.path.relpath(current, bundle)
        names.add(relative_dir)
        info = os.lstat(current)
        if relative_dir not in allowed_dirs or not stat.S_ISDIR(info.st_mode) or info.st_dev != root_info.st_dev or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise InstallError("unknown or unsafe directory")
        dirs.append(ManifestDirectory(relative_dir, _file_identity(info)))
        for name in filenames:
            path = Path(current) / name
            relative = path.relative_to(bundle).as_posix()
            names.add(relative)
            file_info = os.lstat(path)
            if relative not in allowed_files or not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1 or file_info.st_dev != root_info.st_dev or file_info.st_uid != uid or stat.S_IMODE(file_info.st_mode) & 0o022:
                raise InstallError("unknown or unsafe file")
            descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
            try:
                if _file_identity(os.fstat(descriptor)) != _file_identity(file_info):
                    raise InstallError("検証後に変更されました")
                body = b""
                while chunk := os.read(descriptor, 65536):
                    body += chunk
            finally:
                os.close(descriptor)
            files.append(ManifestFile(relative, _file_identity(file_info), len(body), hashlib.sha256(body).hexdigest()))
    if names != allowed_files | allowed_dirs:
        raise InstallError("bundle allowlist mismatch")
    # The tree digest identifies deployable content, not source inodes.  Per-file
    # identities above still pin the source against replacement during copy.
    canonical = [(item.path, item.identity.mode) for item in dirs]
    canonical += [(item.path, item.identity.mode, item.size, item.sha256) for item in files]
    return BundleManifest(_file_identity(root_info), tuple(sorted(dirs, key=lambda x: x.path)),
                          tuple(sorted(files, key=lambda x: x.path)), hashlib.sha256(_canonical(canonical)).hexdigest())


def copy_manifest_tree(manifest: BundleManifest, source: Path, transaction_fd: int) -> None:
    source = Path(source)
    current_root = os.lstat(source)
    if _file_identity(current_root) != manifest.source_root:
        raise InstallError("検証後に変更されました")
    opened = {".": os.dup(transaction_fd)}
    try:
        for directory in manifest.directories:
            if directory.path == ".":
                continue
            parent, name = os.path.split(directory.path)
            os.mkdir(name, directory.identity.mode, dir_fd=opened[parent or "."])
            opened[directory.path] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW,
                                             dir_fd=opened[parent or "."])
        for item in manifest.files:
            src = source / item.path
            before = os.lstat(src)
            descriptor = os.open(src, os.O_RDONLY | _NOFOLLOW)
            try:
                after = os.fstat(descriptor)
                if _file_identity(before) != item.identity or _file_identity(after) != item.identity or after.st_size != item.size:
                    raise InstallError("検証後に変更されました")
                body = b""
                while chunk := os.read(descriptor, 65536):
                    body += chunk
            finally:
                os.close(descriptor)
            if hashlib.sha256(body).hexdigest() != item.sha256:
                raise InstallError("検証後に変更されました")
            parent, name = os.path.split(item.path)
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                          item.identity.mode, dir_fd=opened[parent or "."])
            try:
                os.write(out, body)
                os.fchmod(out, item.identity.mode)
                os.fsync(out)
            finally:
                os.close(out)
        for descriptor in reversed(tuple(opened.values())):
            os.fsync(descriptor)
    finally:
        for descriptor in opened.values():
            os.close(descriptor)


def tree_digest_fd(root_fd: int) -> str:
    """Compute the content/mode tree digest used by BundleManifest via dirfds."""
    directories: list[tuple[str, int]] = []
    files: list[tuple[str, int, int, str]] = []

    def scan(fd: int, prefix: str) -> None:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RecoveryError("transactionを隔離して再実行してください")
        directories.append((prefix or ".", stat.S_IMODE(info.st_mode)))
        for name in sorted(os.listdir(fd)):
            if name in (".", "..") or "/" in name or "\0" in name:
                raise RecoveryError("transactionを隔離して再実行してください")
            relative = f"{prefix}/{name}" if prefix else name
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if entry.st_uid != os.getuid():
                raise RecoveryError("transactionを隔離して再実行してください")
            if stat.S_ISDIR(entry.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=fd)
                try:
                    scan(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1:
                child = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
                try:
                    body = _read_all(child)
                finally:
                    os.close(child)
                files.append((relative, stat.S_IMODE(entry.st_mode), len(body),
                              hashlib.sha256(body).hexdigest()))
            else:
                raise RecoveryError("transactionを隔離して再実行してください")
    scan(root_fd, "")
    return hashlib.sha256(_canonical(sorted(directories) + sorted(files))).hexdigest()


def load_renameatx(libc=None):
    library = ctypes.CDLL(None, use_errno=True) if libc is None else libc
    try:
        function = library.renameatx_np
    except AttributeError as exc:
        raise InstallError("安全なrenameatx_npを利用できません") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function


def _renameatx(src_fd: int, src: bytes, dst_fd: int, dst: bytes, flag: int, libc=None) -> None:
    if not isinstance(src, bytes) or not isinstance(dst, bytes):
        raise TypeError("name must be bytes")
    if b"\0" in src or b"\0" in dst:
        raise ValueError("NUL is not allowed")
    function = load_renameatx(libc)
    ctypes.set_errno(0)
    result = function(src_fd, src, dst_fd, dst, flag)
    if result == 0:
        return
    number = ctypes.get_errno()  # Must be captured before any other syscall.
    if number == errno.EEXIST:
        reason = "collision"
    elif number == errno.ENOENT:
        reason = "race"
    elif number in (errno.ENOTSUP, errno.EINVAL):
        reason = "unsupported"
    elif number == errno.EXDEV:
        reason = "mount boundary"
    else:
        reason = "syscall failure"
    raise InstallError(reason)


def rename_swap(src_dir_fd: int, src: bytes, dst_dir_fd: int, dst: bytes, *, libc=None) -> None:
    _renameatx(src_dir_fd, src, dst_dir_fd, dst, RENAME_SWAP, libc)


def rename_exclusive(src_dir_fd: int, src: bytes, dst_dir_fd: int, dst: bytes, *, libc=None) -> None:
    _renameatx(src_dir_fd, src, dst_dir_fd, dst, RENAME_EXCL, libc)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _envelope(payload: dict) -> bytes:
    return _canonical({"payload": payload, "checksum": hashlib.sha256(_canonical(payload)).hexdigest()}) + b"\n"


def _decode_envelope(body: bytes, txn_id: str, generation: int) -> dict:
    try:
        value = json.loads(body)
        payload = value["payload"]
        checksum = value["checksum"]
        valid = (set(value) == {"payload", "checksum"} and
                 isinstance(payload, dict) and
                 hashlib.sha256(_canonical(payload)).hexdigest() == checksum and
                 payload["schema"] == 1 and payload["txn_id"] == txn_id and
                 payload["generation"] == generation)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        valid = False
    if not valid:
        raise RecoveryError("transactionを隔離して再実行してください")
    return payload


def _write_exclusive(fd: int, name: str, body: bytes) -> None:
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=fd)
    try:
        offset = 0
        while offset < len(body):
            offset += os.write(out, body[offset:])
        os.fchmod(out, 0o600)
        os.fsync(out)
    finally:
        os.close(out)


class Journal:
    """Append-only state and generation pointers; there is no mutable head."""

    def __init__(self, directory_fd: int, txn_id: str, *, rename: Callable | None = None):
        if len(txn_id) != 32 or any(c not in "0123456789abcdef" for c in txn_id):
            raise ValueError("invalid transaction id")
        self.fd = directory_fd
        self.txn_id = txn_id
        self._rename = rename or rename_exclusive

    def _entries(self) -> set[str]:
        return set(os.listdir(self.fd))

    def _next_generation(self) -> int:
        generations = [int(n[6:-5]) for n in self._entries()
                       if n.startswith("state.") and n.endswith(".json") and n[6:-5].isdigit()]
        return max(generations, default=0) + 1

    def write_state_only(self, phase: str, details: dict) -> dict:
        generation = self._next_generation()
        payload = {"schema": 1, "txn_id": self.txn_id, "generation": generation,
                   "phase": phase, "details": details}
        temp = f"state.{generation}.tmp"
        final = f"state.{generation}.json"
        _write_exclusive(self.fd, temp, _envelope(payload))
        self._rename(self.fd, os.fsencode(temp), self.fd, os.fsencode(final))
        os.fsync(self.fd)
        return payload

    def _write_pointer(self, payload: dict) -> None:
        generation = payload["generation"]
        checksum = hashlib.sha256(_canonical(payload)).hexdigest()
        pointer = _canonical({"txn_id": self.txn_id, "generation": generation,
                              "state_checksum": checksum}) + b"\n"
        temp, final = f"current.{generation}.tmp", f"current.{generation}"
        _write_exclusive(self.fd, temp, pointer)
        self._rename(self.fd, os.fsencode(temp), self.fd, os.fsencode(final))
        os.fsync(self.fd)
        readback = self._read_regular(final)
        if readback != pointer:
            raise RecoveryError("transactionを隔離して再実行してください")

    def append(self, phase: str, details: dict) -> dict:
        payload = self.write_state_only(phase, details)
        self._write_pointer(payload)
        return payload

    def _read_regular(self, name: str) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=self.fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise RecoveryError("transactionを隔離して再実行してください")
            return os.read(descriptor, 65537)
        finally:
            os.close(descriptor)

    def recover_head(self) -> dict:
        entries = self._entries()
        allowed = {
            name for name in entries
            if ((name.startswith("state.") and name.endswith(".json") and name[6:-5].isdigit())
                or (name.startswith("current.") and name[8:].isdigit())
                or name in {"staged.app", "trash.app"})
        }
        if entries != allowed:
            raise RecoveryError("transactionを隔離して再実行してください")
        state_nums = sorted(int(n[6:-5]) for n in entries if n.startswith("state.") and n.endswith(".json") and n[6:-5].isdigit())
        pointer_nums = sorted(int(n[8:]) for n in entries if n.startswith("current.") and n[8:].isdigit())
        if not state_nums or state_nums != list(range(1, max(state_nums) + 1)):
            raise RecoveryError("transactionを隔離して再実行してください")
        if pointer_nums not in (state_nums, state_nums[:-1]):
            raise RecoveryError("transactionを隔離して再実行してください")
        states = {}
        for generation in state_nums:
            states[generation] = _decode_envelope(self._read_regular(f"state.{generation}.json"), self.txn_id, generation)
        for generation in pointer_nums:
            try:
                pointer = json.loads(self._read_regular(f"current.{generation}"))
                expected = {"txn_id": self.txn_id, "generation": generation,
                            "state_checksum": hashlib.sha256(_canonical(states[generation])).hexdigest()}
            except (ValueError, json.JSONDecodeError):
                raise RecoveryError("transactionを隔離して再実行してください") from None
            if pointer != expected:
                raise RecoveryError("transactionを隔離して再実行してください")
        highest = states[state_nums[-1]]
        if pointer_nums == state_nums[:-1]:
            self._write_pointer(highest)
        return highest


@dataclass(frozen=True)
class RecoveryPlan:
    actions: tuple[str, ...]
    result: tuple[str | None, str]


_PLANS = {
    ("a", "absent"): (("DROP_TEMP",), ("old", "absent")),
    ("a", "old"): (("DROP_TEMP",), ("old", "old")),
    ("a", "new"): (("OLD_TO_BACKUP", "PUBLISH_TEMP", "DROP_BACKUP"), ("new", "new")),
    ("b", "absent"): (("RESTORE_BACKUP", "DROP_TEMP"), ("old", "absent")),
    ("b", "old"): (("RESTORE_BACKUP", "DROP_TEMP"), ("old", "old")),
    ("b", "new"): (("PUBLISH_TEMP", "DROP_BACKUP"), ("new", "new")),
    ("c", "absent"): (("NEW_TO_TRASH", "RESTORE_BACKUP", "DROP_TRASH"), ("old", "absent")),
    ("c", "old"): (("NEW_TO_TRASH", "RESTORE_BACKUP", "DROP_TRASH"), ("old", "old")),
    ("c", "new"): (("DROP_BACKUP",), ("new", "new")),
    ("d", "absent"): (("DROP_TEMP",), (None, "absent")),
    ("d", "old"): (("DROP_TEMP",), (None, "old")),
    ("d", "new"): (("PUBLISH_TEMP",), ("new", "new")),
    ("e", "absent"): (("NEW_TO_TRASH", "DROP_TRASH"), (None, "absent")),
    ("e", "old"): (("NEW_TO_TRASH", "DROP_TRASH"), (None, "old")),
    ("e", "new"): ((), ("new", "new")),
}


def classify_config_state(config_state: str, app_state: str) -> RecoveryPlan:
    try:
        actions, result = _PLANS[(config_state, app_state)]
    except KeyError as exc:
        raise RecoveryError("transactionを隔離して再実行してください") from exc
    return RecoveryPlan(actions, result)


def recovery_actions(config_state: str, app_state: str) -> tuple[str, ...]:
    return classify_config_state(config_state, app_state).actions


_ACTION_NAMES = {
    "OLD_TO_BACKUP": ("config.json", "backup"),
    "PUBLISH_TEMP": ("temp", "config.json"),
    "NEW_TO_TRASH": ("config.json", "trash"),
    "RESTORE_BACKUP": ("backup", "config.json"),
}
_DROP_NAMES = {"DROP_TEMP": "temp", "DROP_BACKUP": "backup", "DROP_TRASH": "trash"}


def execute_recovery_action(directory_fd: int, action: str, *, crash_after_mutation: bool = False) -> None:
    """Apply one fixed recovery mutation and safely accept its post-state.

    The durable journal which selected the action owns identity validation. This
    primitive is deliberately idempotent for a crash after mutation+fsync.
    """
    names = set(os.listdir(directory_fd))
    if action in _ACTION_NAMES:
        source, destination = _ACTION_NAMES[action]
        if source in names and destination not in names:
            rename_exclusive(directory_fd, os.fsencode(source), directory_fd, os.fsencode(destination))
            os.fsync(directory_fd)
            if crash_after_mutation:
                raise RuntimeError("fault after mutation fsync")
            return
        if source not in names and destination in names:
            return
    elif action in _DROP_NAMES:
        name = _DROP_NAMES[action]
        if name in names:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RecoveryError("transactionを隔離して再実行してください")
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            if crash_after_mutation:
                raise RuntimeError("fault after mutation fsync")
            return
        return
    else:
        raise RecoveryError("transactionを隔離して再実行してください")
    raise RecoveryError("transactionを隔離して再実行してください")


@dataclass(frozen=True)
class CleanupEntry:
    path: str
    dev: int
    ino: int
    kind: str
    mode: int
    size: int | None = None
    sha256: str | None = None


def _cleanup_error() -> RecoveryError:
    return RecoveryError("cleanup対象を隔離して再実行してください")


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _scan_cleanup_tree(root_fd: int, prefix: str = "") -> list[CleanupEntry]:
    """Return an exact, no-follow inventory beneath an already verified dirfd."""
    result: list[CleanupEntry] = []
    for name in sorted(os.listdir(root_fd)):
        if name in (".", "..") or "/" in name or "\0" in name:
            raise _cleanup_error()
        relative = f"{prefix}/{name}" if prefix else name
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if info.st_uid != os.getuid():
            raise _cleanup_error()
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise _cleanup_error()
            descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=root_fd)
            try:
                opened = os.fstat(descriptor)
                if _file_identity(opened) != _file_identity(info) or opened.st_size != info.st_size:
                    raise _cleanup_error()
                body = _read_all(descriptor)
            finally:
                os.close(descriptor)
            result.append(CleanupEntry(relative, info.st_dev, info.st_ino, "file",
                                       stat.S_IMODE(info.st_mode),
                                       len(body), hashlib.sha256(body).hexdigest()))
        elif stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=root_fd)
            try:
                opened = os.fstat(descriptor)
                if _file_identity(opened) != _file_identity(info):
                    raise _cleanup_error()
                result.extend(_scan_cleanup_tree(descriptor, relative))
            finally:
                os.close(descriptor)
            result.append(CleanupEntry(relative, info.st_dev, info.st_ino, "dir",
                                       stat.S_IMODE(info.st_mode)))
        else:
            raise _cleanup_error()
    return result


def capture_cleanup_inventory(root_fd: int) -> tuple[CleanupEntry, tuple[CleanupEntry, ...]]:
    """Capture the root and complete child inventory from a previously verified dirfd."""
    root = os.fstat(root_fd)
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid():
        raise _cleanup_error()
    root_entry = CleanupEntry(".", root.st_dev, root.st_ino, "dir",
                              stat.S_IMODE(root.st_mode))
    return root_entry, tuple(_scan_cleanup_tree(root_fd))


def create_cleanup_receipt(parent_fd: int, transaction_name: str, txn_id: str,
                           expected_root: CleanupEntry,
                           expected_entries: tuple[CleanupEntry, ...]) -> str:
    """Durably record only a caller-verified exact transaction inventory."""
    if transaction_name != f"transaction.{txn_id}" or len(txn_id) != 32 or any(c not in "0123456789abcdef" for c in txn_id):
        raise ValueError("invalid cleanup transaction")
    root_fd = os.open(transaction_name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    try:
        root = os.fstat(root_fd)
        if root.st_uid != os.getuid():
            raise _cleanup_error()
        current_root, entries = capture_cleanup_inventory(root_fd)
        if current_root != expected_root or tuple(entries) != tuple(expected_entries):
            raise _cleanup_error()
    finally:
        os.close(root_fd)
    payload = {
        "schema": 1, "txn_id": txn_id, "transaction_name": transaction_name,
        "transaction": {"dev": expected_root.dev, "ino": expected_root.ino},
        "entries": [entry.__dict__ for entry in entries],
    }
    name = f"cleanup.{txn_id}.json"
    _write_exclusive(parent_fd, name, _envelope(payload))
    os.fsync(parent_fd)
    return name


def _load_cleanup_receipt(parent_fd: int, receipt_name: str) -> dict:
    descriptor = os.open(receipt_name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise _cleanup_error()
        body = _read_all(descriptor)
    finally:
        os.close(descriptor)
    try:
        envelope = json.loads(body)
        payload = envelope["payload"]
        valid = (set(envelope) == {"payload", "checksum"} and
                 hashlib.sha256(_canonical(payload)).hexdigest() == envelope["checksum"] and
                 payload["schema"] == 1 and receipt_name == f"cleanup.{payload['txn_id']}.json" and
                 payload["transaction_name"] == f"transaction.{payload['txn_id']}" and
                 isinstance(payload["entries"], list))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        valid = False
    if not valid:
        raise _cleanup_error()
    return payload


def resume_cleanup(parent_fd: int, receipt_name: str, *, fault: Callable[[str, str], None] | None = None) -> None:
    """Resume identity-bound fd-relative cleanup; the receipt is removed last."""
    payload = _load_cleanup_receipt(parent_fd, receipt_name)
    transaction_name = payload["transaction_name"]
    try:
        root_fd = os.open(transaction_name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        root_fd = None
    if root_fd is not None:
        try:
            root = os.fstat(root_fd)
            if {"dev": root.st_dev, "ino": root.st_ino} != payload["transaction"] or root.st_uid != os.getuid():
                raise _cleanup_error()
            recorded = {}
            for raw in payload["entries"]:
                entry = CleanupEntry(**raw)
                if entry.path in recorded or entry.kind not in ("file", "dir") or entry.path.startswith("/") or ".." in entry.path.split("/"):
                    raise _cleanup_error()
                recorded[entry.path] = entry
            # Validate the complete surviving tree before performing the first deletion.
            actual = {entry.path: entry for entry in _scan_cleanup_tree(root_fd)}
            if not set(actual).issubset(recorded):
                raise _cleanup_error()
            for path, entry in actual.items():
                if entry != recorded[path]:
                    raise _cleanup_error()
            for raw in payload["entries"]:
                entry = CleanupEntry(**raw)
                components = entry.path.split("/")
                descriptors = [os.dup(root_fd)]
                try:
                    missing = False
                    for component in components[:-1]:
                        try:
                            descriptors.append(os.open(component, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW,
                                                       dir_fd=descriptors[-1]))
                        except FileNotFoundError:
                            missing = True
                            break
                    if missing:
                        continue
                    parent = descriptors[-1]
                    name = components[-1]
                    try:
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if (info.st_dev, info.st_ino) != (entry.dev, entry.ino):
                        raise _cleanup_error()
                    if entry.kind == "file":
                        os.unlink(name, dir_fd=parent)
                    else:
                        os.rmdir(name, dir_fd=parent)
                    os.fsync(parent)
                    if fault:
                        fault(entry.kind, entry.path)
                finally:
                    for descriptor in reversed(descriptors):
                        os.close(descriptor)
        finally:
            os.close(root_fd)
        os.rmdir(transaction_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        if fault:
            fault("transaction", transaction_name)
    os.unlink(receipt_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


_COMPLETION_KEYS = {"schema_version", "transaction_id", "transaction_state", "cleanup_state",
                    "app_tree_sha256", "config_sha256", "completed_at", "result_classification"}


def _completion_error() -> RecoveryError:
    return RecoveryError("完了レシートを検証できませんでした")


def _completion_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise _completion_error() from exc


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def write_completion_receipt(parent_fd: int, txn_id: str, app_tree_sha256: str,
                             config_sha256: str, *, completed_at: str | None = None,
                             fault: Callable[[str], None] | None = None) -> str:
    """Durably publish a fixed, non-secret completion result before cleanup."""
    if (not re.fullmatch(r"[0-9a-f]{32}", txn_id)
            or not re.fullmatch(r"[0-9a-f]{64}", app_tree_sha256)
            or not (config_sha256 == "absent" or re.fullmatch(r"[0-9a-f]{64}", config_sha256))):
        raise ValueError("invalid completion receipt")
    timestamp = completed_at or datetime.now(timezone.utc).isoformat()
    _completion_time(timestamp)
    try:
        os.mkdir("completed", 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    completed_fd = os.open("completed", os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    temp_name, final_name = f".{txn_id}.tmp", f"{txn_id}.json"
    try:
        info = os.fstat(completed_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise _completion_error()
        payload = {"schema_version": 1, "transaction_id": txn_id,
                   "transaction_state": "COMMITTED", "cleanup_state": "CLEANED",
                   "app_tree_sha256": app_tree_sha256, "config_sha256": config_sha256,
                   "completed_at": timestamp, "result_classification": "INSTALLATION_COMPLETED"}
        try:
            stale = os.stat(temp_name, dir_fd=completed_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(stale.st_mode) or stale.st_uid != os.getuid()
                    or stat.S_IMODE(stale.st_mode) != 0o600 or stale.st_nlink != 1):
                raise _completion_error()
            os.unlink(temp_name, dir_fd=completed_fd)
            os.fsync(completed_fd)
        descriptor = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                             0o600, dir_fd=completed_fd)
        try:
            body = _canonical(payload) + b"\n"
            if os.write(descriptor, body) != len(body):
                raise _completion_error()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if fault:
            fault("before_rename")
        os.rename(temp_name, final_name, src_dir_fd=completed_fd, dst_dir_fd=completed_fd)
        os.fsync(completed_fd)
        if fault:
            fault("after_rename")
        return final_name
    finally:
        os.close(completed_fd)


def verify_latest_completion_receipt(parent_fd: int, app_tree_sha256: str,
                                     config_sha256: str, *, now: str | None = None,
                                     max_age_seconds: int = 86400) -> dict:
    """Validate the latest owner-only receipt against freshly computed hashes."""
    parent_entries = set(os.listdir(parent_fd))
    if any(name not in {"lock", "completed"} for name in parent_entries):
        raise _completion_error()
    try:
        completed_fd = os.open("completed", os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW,
                               dir_fd=parent_fd)
    except OSError as exc:
        raise _completion_error() from exc
    try:
        directory = os.fstat(completed_fd)
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise _completion_error()
        candidates = []
        completed_entries = os.listdir(completed_fd)
        if any(not re.fullmatch(r"[0-9a-f]{32}[.]json", name) for name in completed_entries):
            raise _completion_error()
        for name in completed_entries:
            try:
                descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=completed_fd)
            except OSError as exc:
                raise _completion_error() from exc
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise _completion_error()
                raw = _read_all(descriptor)
            finally:
                os.close(descriptor)
            try:
                payload = json.loads(raw, object_pairs_hook=_strict_object)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise _completion_error() from exc
            if (type(payload) is not dict or set(payload) != _COMPLETION_KEYS
                    or payload.get("schema_version") != 1
                    or payload.get("transaction_id") + ".json" != name
                    or payload.get("transaction_state") != "COMMITTED"
                    or payload.get("cleanup_state") != "CLEANED"
                    or payload.get("result_classification") != "INSTALLATION_COMPLETED"
                    or not re.fullmatch(r"[0-9a-f]{64}", payload.get("app_tree_sha256", ""))
                    or not (payload.get("config_sha256") == "absent" or
                            re.fullmatch(r"[0-9a-f]{64}", payload.get("config_sha256", "")))):
                raise _completion_error()
            candidates.append((_completion_time(payload["completed_at"]), payload))
        if not candidates:
            raise _completion_error()
        timestamp, latest = max(candidates, key=lambda item: item[0])
        age = (_completion_time(now or datetime.now(timezone.utc).isoformat()) - timestamp).total_seconds()
        if age < 0 or age > max_age_seconds:
            raise _completion_error()
        if latest["app_tree_sha256"] != app_tree_sha256 or latest["config_sha256"] != config_sha256:
            raise _completion_error()
        return latest
    finally:
        os.close(completed_fd)
