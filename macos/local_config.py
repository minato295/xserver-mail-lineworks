"""Secure loading of the macOS launcher's local configuration."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath


MAX_FILE_SIZE = 64 * 1024
KEY_TO_ENV = {
    "servername": "XSERVER_SERVERNAME",
    "command_path": "XSERVER_COMMAND_PATH",
    "ftps_host": "XSERVER_FTPS_HOST",
    "config_path": "XSERVER_CONFIG_PATH",
    "filesystem_home": "XSERVER_HOME",
    "ssh_alias": "XSERVER_SSH_ALIAS",
}
CONFIG_KEYS = frozenset(KEY_TO_ENV)
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_SSH_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class LocalConfigError(RuntimeError):
    """Raised when local configuration cannot be trusted or validated."""


def _fail(reason: str) -> LocalConfigError:
    return LocalConfigError(f"Local configuration rejected: {reason}")


def _lstat(path: Path, description: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise _fail(f"cannot inspect {description}") from exc


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        stat.S_IMODE(info.st_mode),
        stat.S_IFMT(info.st_mode),
    )


def _check_directory_info(
    info: os.stat_result, uid: int, description: str, *, exact_mode: int | None = None
) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise _fail(f"unsafe {description} type")
    if info.st_uid != uid:
        raise _fail(f"unsafe {description} owner")
    permissions = stat.S_IMODE(info.st_mode)
    if exact_mode is not None:
        if permissions != exact_mode:
            raise _fail(f"unsafe {description} permissions")
    elif permissions & 0o022:
        raise _fail(f"unsafe {description} permissions")


def _open_directory(
    name: str | Path,
    uid: int,
    description: str,
    *,
    dir_fd: int | None = None,
    exact_mode: int | None = None,
) -> int:
    try:
        if dir_fd is None:
            before = _lstat(Path(name), description)
        else:
            before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise _fail(f"cannot open {description}") from exc
    try:
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            raise _fail(f"{description} changed during opening")
        _check_directory_info(after, uid, description, exact_mode=exact_mode)
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_trusted_file(app_fd: int, uid: int) -> bytes:
    try:
        before = os.stat("config.json", dir_fd=app_fd, follow_symlinks=False)
    except OSError as exc:
        raise _fail("cannot inspect configuration file") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _fail("unsafe configuration file type")
    if before.st_uid != uid:
        raise _fail("unsafe configuration file owner")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise _fail("unsafe configuration file permissions")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open("config.json", flags, dir_fd=app_fd)
    except OSError as exc:
        raise _fail("cannot open configuration file") from exc
    try:
        after = os.fstat(fd)
        if _identity(after) != _identity(before):
            raise _fail("configuration file changed during opening")
        chunks: list[bytes] = []
        remaining = MAX_FILE_SIZE + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise _fail("cannot read configuration file") from exc
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_SIZE:
        raise _fail("configuration file is too large")
    return data


def _is_dns_hostname(value: str) -> bool:
    if len(value) > 253 or value.endswith("."):
        return False
    labels = value.split(".")
    return len(labels) >= 2 and all(_DNS_LABEL.fullmatch(label) for label in labels)


def _validate_path(value: str) -> bool:
    if not value.startswith("/") or "//" in value:
        return False
    parts = value.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if any(part.casefold() == "public_html" for part in parts):
        return False
    return PurePosixPath(value).is_absolute()


def validate_config(value: object) -> dict[str, str]:
    """Validate and copy a macOS launcher configuration."""
    if not isinstance(value, dict) or set(value) != CONFIG_KEYS:
        raise _fail("invalid configuration schema")
    for key, item in value.items():
        if not isinstance(item, str) or not item:
            raise _fail("invalid configuration value")
        if any(unicodedata.category(char) == "Cc" for char in item):
            raise _fail("invalid configuration value")
    if not _is_dns_hostname(value["servername"]) or not value["servername"].lower().endswith(
        (".xsrv.jp", ".xbiz.jp")
    ):
        raise _fail("invalid server name")
    if not _is_dns_hostname(value["ftps_host"]):
        raise _fail("invalid FTPS host")
    if _SSH_ALIAS.fullmatch(value["ssh_alias"]) is None or "*" in value["ssh_alias"]:
        raise _fail("invalid SSH alias")
    for key in ("command_path", "config_path", "filesystem_home"):
        if not _validate_path(value[key]):
            raise _fail("invalid path")
    return dict(value)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_config_with_digest(path: Path, uid: int) -> tuple[dict[str, str], str]:
    """Load and validate config, returning a digest of the same verified bytes."""
    path = Path(path)
    home = Path.home()
    expected = home / "Library" / "Application Support" / "XserverMailLineworks" / "config.json"
    if not path.is_absolute() or path != expected:
        raise _fail("invalid configuration location")

    directory_fds: list[int] = []
    try:
        home_fd = _open_directory(home, uid, "home directory")
        directory_fds.append(home_fd)
        library_fd = _open_directory("Library", uid, "Library directory", dir_fd=home_fd)
        directory_fds.append(library_fd)
        support_fd = _open_directory(
            "Application Support", uid, "Application Support directory", dir_fd=library_fd
        )
        directory_fds.append(support_fd)
        app_fd = _open_directory(
            "XserverMailLineworks",
            uid,
            "application directory",
            dir_fd=support_fd,
            exact_mode=0o700,
        )
        directory_fds.append(app_fd)
        raw = _read_trusted_file(app_fd, uid)
    finally:
        for fd in reversed(directory_fds):
            os.close(fd)
    try:
        decoded = raw.decode("utf-8")
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _fail("invalid JSON") from exc
    return validate_config(parsed), hashlib.sha256(raw).hexdigest()


def load_config(path: Path, uid: int) -> dict[str, str]:
    """Load a trusted launcher configuration owned by ``uid``."""
    return load_config_with_digest(path, uid)[0]


def to_environment(config: dict[str, str]) -> dict[str, str]:
    """Return only the fixed environment mapping for a validated config."""
    return {environment: config[key] for key, environment in KEY_TO_ENV.items()}
