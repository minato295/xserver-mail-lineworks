"""Validate the installed Python and start the bundled manager safely."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    from .local_config import load_config, to_environment
except ImportError:  # Direct execution from an application Resources directory.
    from local_config import load_config, to_environment


@dataclass(frozen=True)
class RuntimeRecord:
    path: str
    device: int
    inode: int
    major: int
    minor: int


def _reject(reason: str) -> RuntimeError:
    return RuntimeError(f"Python runtime rejected: {reason}")


def _validated_python_version(version_info: tuple[int, int]) -> tuple[int, int]:
    if (
        type(version_info) is not tuple
        or len(version_info) != 2
        or any(type(component) is not int for component in version_info)
        or version_info not in {(3, 13), (3, 14)}
    ):
        raise _reject("unsupported version")
    return version_info


def _validate_runtime_trust(info: os.stat_result) -> None:
    """Reject a Python file writable by an account that can run this process."""
    try:
        uid = os.getuid()
        groups = set(os.getgroups())
        groups.add(os.getgid())
    except OSError as exc:
        raise _reject("cannot inspect executable trust") from exc
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _reject("unsafe executable type")
    if info.st_uid not in {0, uid}:
        raise _reject("unsafe executable owner")
    if mode & (stat.S_ISUID | stat.S_ISGID):
        raise _reject("unsafe executable permissions")
    if mode & stat.S_IWOTH:
        raise _reject("unsafe executable permissions")
    if mode & stat.S_IWGRP and info.st_gid in groups:
        raise _reject("unsafe executable permissions")


def record_python_runtime(
    executable: str, version_info: tuple[int, int]
) -> RuntimeRecord:
    """Record a supported Python executable's canonical filesystem identity."""
    version = _validated_python_version(version_info)
    if type(executable) is not str or not os.path.isabs(executable):
        raise _reject("executable is not canonical")
    try:
        canonical = str(Path(executable).resolve(strict=True))
        info = os.lstat(executable)
    except (OSError, RuntimeError) as exc:
        raise _reject("cannot inspect executable") from exc
    if executable != canonical:
        raise _reject("executable is not canonical")
    _validate_runtime_trust(info)
    return RuntimeRecord(canonical, info.st_dev, info.st_ino, *version)


def validate_runtime_record(
    record: RuntimeRecord, stat_result: os.stat_result, version_info: tuple[int, int]
) -> None:
    """Require the recorded Python version and filesystem identity."""
    _validate_runtime_identity(
        record, stat_result.st_dev, stat_result.st_ino, version_info
    )


def _validate_runtime_identity(
    record: RuntimeRecord, device: int, inode: int, version_info: tuple[int, int]
) -> None:
    version = _validated_python_version(version_info)
    _validated_python_version((record.major, record.minor))
    if version != (record.major, record.minor):
        raise _reject("version mismatch")
    if (device, inode) != (record.device, record.inode):
        raise _reject("file identity mismatch")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _load_record(metadata_path: Path) -> RuntimeRecord:
    try:
        if metadata_path.is_symlink() or not stat.S_ISREG(os.lstat(metadata_path).st_mode):
            raise _reject("unsafe metadata")
        value = json.loads(metadata_path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except RuntimeError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _reject("invalid metadata") from exc
    fields = {"path", "device", "inode", "major", "minor"}
    if not isinstance(value, dict) or set(value) != fields:
        raise _reject("invalid metadata schema")
    if type(value["path"]) is not str or not value["path"] or not os.path.isabs(value["path"]):
        raise _reject("invalid metadata path")
    if any(type(value[name]) is not int for name in fields - {"path"}):
        raise _reject("invalid metadata type")
    return RuntimeRecord(**value)


def validate_python_runtime(
    metadata_path: Path, executable: str = sys.executable
) -> RuntimeRecord:
    """Validate executable path, identity and version against installation metadata."""
    record = _load_record(Path(metadata_path))
    current = record_python_runtime(executable, tuple(sys.version_info[:2]))
    if record.path != current.path:
        raise _reject("executable path mismatch")
    _validate_runtime_identity(
        record, current.device, current.inode, (current.major, current.minor)
    )
    return record


def _require_plain_path(path: Path, description: str, *, directory: bool = False) -> None:
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError(f"Unsafe {description} path")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"Unsafe {description} path")
        final = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect {description}") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(final.st_mode):
        raise RuntimeError(f"Unsafe {description} type")


def run_manager(
    resources_dir: Path,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Start only the bundled manager with validated local configuration."""
    resources_dir = Path(resources_dir)
    config_path = Path(config_path)
    manager_path = resources_dir / "manager" / "manage.py"
    _require_plain_path(resources_dir, "resources", directory=True)
    _require_plain_path(manager_path, "manager")
    _require_plain_path(config_path, "configuration")
    config = load_config(config_path, os.getuid())
    merged_env = os.environ.copy()
    merged_env.update(to_environment(config))
    merged_env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = runner(
        [sys.executable, "-B", str(manager_path)],
        env=merged_env,
        check=False,
        shell=False,
    )
    return int(completed.returncode)


def main() -> int:
    resources = Path(__file__).resolve().parent
    config = Path.home() / "Library" / "Application Support" / "XserverMailLineworks" / "config.json"
    try:
        validate_python_runtime(resources / "python-runtime.json")
        return run_manager(resources, config)
    except Exception:
        print("ランチャーの安全性検証または起動に失敗しました。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
