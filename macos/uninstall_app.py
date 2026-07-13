"""Unprivileged removal of the fixed per-user application."""

from __future__ import annotations

import os
import stat
import fcntl
import secrets
import plistlib
from pathlib import Path

from .install_transaction import (Journal, create_cleanup_receipt, rename_exclusive,
                                  resume_cleanup, RecoveryError, tree_digest_fd,
                                  capture_cleanup_inventory)


class UninstallError(RuntimeError):
    pass


def _safe_dir(path: Path, uid: int) -> os.stat_result:
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022):
        raise UninstallError("安全でない配置先です")
    return info


def _snapshot_tree(directory_fd: int, uid: int):
    result = []
    for name in sorted(os.listdir(directory_fd)):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if info.st_uid != uid or stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise UninstallError("アプリのidentityが一致しません。隔離して再実行してください")
        identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size)
        children = None
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                children = _snapshot_tree(child_fd, uid)
            finally:
                os.close(child_fd)
        result.append((name, identity, children))
    return tuple(result)


def _delete_snapshot(directory_fd: int, snapshot) -> None:
    for name, identity, children in snapshot:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        current = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size)
        if current != identity:
            raise UninstallError("アプリのidentityが一致しません。隔離して再実行してください")
        if children is None:
            os.unlink(name, dir_fd=directory_fd)
        else:
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                _delete_snapshot(child_fd, children)
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        os.fsync(directory_fd)


def uninstall(*, remove_config: bool = False, remove_keychain: bool = False,
              uid: int | None = None, _fault=None) -> None:
    if remove_keychain:
        raise UninstallError("キーチェーン項目は自動削除できません")
    real_uid, effective_uid = os.getuid(), os.geteuid()
    if real_uid == 0 or effective_uid == 0 or real_uid != effective_uid:
        raise UninstallError("管理者実行またはUIDの不一致は許可されません")
    uid = os.getuid() if uid is None else uid
    home = Path.home()
    applications = home / "Applications"
    app = applications / "Xserverメール通知管理.app"
    installer_path = applications / ".xserver-mail-lineworks-installer"
    if not (app.exists() or app.is_symlink()) and installer_path.exists():
        _safe_dir(home, uid); _safe_dir(applications, uid); _safe_dir(installer_path, uid)
        applications_fd = os.open(applications, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        installer_fd = os.open(installer_path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            lock_fd = os.open("lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                              0o600, dir_fd=installer_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _recover_uninstall_transactions(applications_fd, installer_fd, uid)
            finally:
                os.close(lock_fd)
        finally:
            os.close(installer_fd); os.close(applications_fd)
    if app.exists() or app.is_symlink():
        _safe_dir(home, uid)
        _safe_dir(applications, uid)
        _safe_dir(app, uid)
        directory_fd = os.open(applications, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            validation_fd = os.open(app.name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                                    dir_fd=directory_fd)
            try:
                _validate_bundle_identifier(validation_fd, uid)
                _snapshot_tree(validation_fd, uid)
                app_tree_sha256 = tree_digest_fd(validation_fd)
            finally:
                os.close(validation_fd)
            try:
                os.mkdir(".xserver-mail-lineworks-installer", 0o700, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileExistsError:
                pass
            installer_fd = os.open(".xserver-mail-lineworks-installer", os.O_RDONLY | os.O_DIRECTORY |
                                   getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                installer_info = os.fstat(installer_fd)
                if installer_info.st_uid != uid or stat.S_IMODE(installer_info.st_mode) != 0o700:
                    raise UninstallError("安全でないinstaller領域です")
                lock_fd = os.open("lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                                  0o600, dir_fd=installer_fd)
                try:
                    lock_info = os.fstat(lock_fd)
                    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != uid or lock_info.st_nlink != 1:
                        raise UninstallError("安全でないlockです")
                    os.fchmod(lock_fd, 0o600)
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise UninstallError("別の処理が実行中です") from exc
                    _recover_uninstall_transactions(directory_fd, installer_fd, uid)
                    if app.name not in os.listdir(directory_fd):
                        return
                    # Re-open and pin the exact source only after the shared lock.
                    locked_manifest = _exact_manifest(app, uid)
                    locked_fd = os.open(app.name, os.O_RDONLY | os.O_DIRECTORY |
                                        getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                    try:
                        locked_info = os.fstat(locked_fd)
                        if ((locked_info.st_dev, locked_info.st_ino) !=
                                (locked_manifest.source_root.dev, locked_manifest.source_root.ino)):
                            raise UninstallError("アプリのidentityが一致しません")
                        _validate_bundle_identifier(locked_fd, uid)
                        app_tree_sha256 = tree_digest_fd(locked_fd)
                    finally:
                        os.close(locked_fd)
                    txn_id = secrets.token_hex(16)
                    txn_name = f"transaction.{txn_id}"
                    os.mkdir(txn_name, 0o700, dir_fd=installer_fd)
                    os.fsync(installer_fd)
                    txn_fd = os.open(txn_name, os.O_RDONLY | os.O_DIRECTORY |
                                     getattr(os, "O_NOFOLLOW", 0), dir_fd=installer_fd)
                    try:
                        journal = Journal(txn_fd, txn_id)
                        details = {"trash": "trash.app", "remove_config": bool(remove_config),
                                   "tree_sha256": app_tree_sha256,
                                   "source_dev": locked_manifest.source_root.dev,
                                   "source_ino": locked_manifest.source_root.ino}
                        journal.append("UNINSTALL_PREPARED", details)
                        if _fault: _fault("after_uninstall_prepared")
                        rename_exclusive(directory_fd, os.fsencode(app.name), txn_fd, b"trash.app")
                        os.fsync(directory_fd); os.fsync(txn_fd)
                        moved = _exact_manifest(installer_path / txn_name / "trash.app", uid)
                        if ((moved.source_root.dev, moved.source_root.ino, moved.tree_sha256) !=
                                (locked_manifest.source_root.dev, locked_manifest.source_root.ino,
                                 locked_manifest.tree_sha256)):
                            raise UninstallError("移動後のアプリidentityが一致しません")
                        if _fault: _fault("after_uninstall_trash_rename")
                        journal.append("UNINSTALL_MOVED", details)
                        if remove_config:
                            _remove_config(home, uid)
                        journal.append("UNINSTALL_CLEANED", details)
                        if _fault: _fault("after_uninstall_cleaned")
                    finally:
                        os.close(txn_fd)
                    cleanup_fd = os.open(txn_name, os.O_RDONLY | os.O_DIRECTORY |
                                         getattr(os, "O_NOFOLLOW", 0), dir_fd=installer_fd)
                    try:
                        expected_root, expected_entries = capture_cleanup_inventory(cleanup_fd)
                    finally:
                        os.close(cleanup_fd)
                    receipt = create_cleanup_receipt(installer_fd, txn_name, txn_id,
                                                     expected_root, expected_entries)
                    resume_cleanup(installer_fd, receipt, fault=_fault)
                finally:
                    os.close(lock_fd)
            finally:
                os.close(installer_fd)
        finally:
            os.close(directory_fd)
    elif remove_config:
        _remove_config(home, uid)


def _validate_bundle_identifier(app_fd: int, uid: int) -> None:
    try:
        contents_fd = os.open("Contents", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=app_fd)
        try:
            plist_fd = os.open("Info.plist", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=contents_fd)
            try:
                info = os.fstat(plist_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1:
                    raise UninstallError("bundle identityが一致しません")
                body = b""
                while chunk := os.read(plist_fd, 65536):
                    body += chunk
            finally:
                os.close(plist_fd)
        finally:
            os.close(contents_fd)
        value = plistlib.loads(body)
        if value.get("CFBundleIdentifier") != "jp.example.xserver-mail-lineworks-manager":
            raise UninstallError("bundle identifierが一致しません")
    except UninstallError:
        raise
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise UninstallError("bundle identityを確認できません") from exc


def _exact_manifest(path: Path, uid: int):
    from .install_app import EXPECTED_BUNDLE_FILES, _ALLOWED_DIRS
    from .install_transaction import create_bundle_manifest
    try:
        return create_bundle_manifest(path, uid, allowed_files=set(EXPECTED_BUNDLE_FILES),
                                      allowed_dirs=set(_ALLOWED_DIRS))
    except Exception as exc:
        raise UninstallError("アプリのexact manifestが一致しません") from exc


def _remove_config(home: Path, uid: int) -> None:
    config_dir = home / "Library/Application Support/XserverMailLineworks"
    config = config_dir / "config.json"
    if config.exists() or config.is_symlink():
        _safe_dir(config_dir, uid)
        info = os.lstat(config)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600:
            raise UninstallError("設定identityが一致しません")
        config.unlink()
        descriptor = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _recover_uninstall_transactions(applications_fd: int, installer_fd: int, uid: int) -> int:
    recovered = 0
    for receipt in sorted(name for name in os.listdir(installer_fd)
                          if name.startswith("cleanup.") and name.endswith(".json")):
        resume_cleanup(installer_fd, receipt)
        recovered += 1
    for name in sorted(os.listdir(installer_fd)):
        if not name.startswith("transaction."):
            continue
        txn_id = name.removeprefix("transaction.")
        if len(txn_id) != 32:
            raise UninstallError("transactionを隔離して再実行してください")
        txn_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=installer_fd)
        abort_prepared = False
        try:
            head = Journal(txn_fd, txn_id).recover_head()
            if not head["phase"].startswith("UNINSTALL_"):
                continue
            details = head["details"]
            if (set(details) != {"trash", "remove_config", "tree_sha256", "source_dev", "source_ino"} or
                    details["trash"] != "trash.app" or
                    not isinstance(details["tree_sha256"], str) or len(details["tree_sha256"]) != 64):
                raise UninstallError("transactionを隔離して再実行してください")
            app_exists = "Xserverメール通知管理.app" in os.listdir(applications_fd)
            trash_exists = "trash.app" in os.listdir(txn_fd)

            def validate_recorded(parent_fd: int, child: str, path: Path) -> None:
                manifest = _exact_manifest(path, uid)
                descriptor = os.open(child, os.O_RDONLY | os.O_DIRECTORY |
                                     getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                try:
                    _validate_bundle_identifier(descriptor, uid)
                    opened = os.fstat(descriptor)
                    actual = (opened.st_dev, opened.st_ino, tree_digest_fd(descriptor))
                    expected = (details["source_dev"], details["source_ino"], details["tree_sha256"])
                    if actual != expected:
                        raise UninstallError("アプリのidentityが一致しません。隔離して再実行してください")
                finally:
                    os.close(descriptor)

            if trash_exists:
                validate_recorded(txn_fd, "trash.app", Path.home() / "Applications" /
                                  ".xserver-mail-lineworks-installer" / name / "trash.app")
            if head["phase"] == "UNINSTALL_PREPARED":
                if app_exists and not trash_exists:
                    validate_recorded(applications_fd, "Xserverメール通知管理.app",
                                      Path.home() / "Applications/Xserverメール通知管理.app")
                    # There is no macOS primitive that conditionally renames a
                    # path only if its verified inode is still present.  Never
                    # mutate the public path during recovery; end this intent
                    # and require a fresh lock-held uninstall validation.
                    Journal(txn_fd, txn_id).append("UNINSTALL_ABORTED", details)
                    abort_prepared = True
                elif not app_exists and trash_exists:
                    pass
                else:
                    raise UninstallError("transactionを隔離して再実行してください")
                if not abort_prepared:
                    Journal(txn_fd, txn_id).append("UNINSTALL_MOVED", details)
            elif head["phase"] == "UNINSTALL_ABORTED":
                if not app_exists or trash_exists:
                    raise UninstallError("transactionを隔離して再実行してください")
                validate_recorded(applications_fd, "Xserverメール通知管理.app",
                                  Path.home() / "Applications/Xserverメール通知管理.app")
                abort_prepared = True
            elif app_exists or (head["phase"] not in {"UNINSTALL_MOVED", "UNINSTALL_CLEANED"}):
                raise UninstallError("transactionを隔離して再実行してください")
            if not abort_prepared and details["remove_config"]:
                _remove_config(Path.home(), uid)
            if not abort_prepared:
                Journal(txn_fd, txn_id).append("UNINSTALL_CLEANED", details)
        except RecoveryError as exc:
            raise UninstallError(str(exc)) from exc
        finally:
            os.close(txn_fd)
        cleanup_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY |
                             getattr(os, "O_NOFOLLOW", 0), dir_fd=installer_fd)
        try:
            expected_root, expected_entries = capture_cleanup_inventory(cleanup_fd)
        finally:
            os.close(cleanup_fd)
        receipt = create_cleanup_receipt(installer_fd, name, txn_id,
                                         expected_root, expected_entries)
        resume_cleanup(installer_fd, receipt)
        recovered += 1
        if abort_prepared:
            raise UninstallError("アンインストールを再実行してください")
    return recovered
