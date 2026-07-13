import ctypes
import errno
import json
import os
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from macos.install_transaction import (
    create_bundle_manifest,
    copy_manifest_tree,
    InstallError,
    Journal,
    RecoveryError,
    RENAME_EXCL,
    RENAME_SWAP,
    classify_config_state,
    load_renameatx,
    recovery_actions,
    execute_recovery_action,
    create_cleanup_receipt,
    capture_cleanup_inventory,
    resume_cleanup,
    rename_exclusive,
    rename_swap,
    write_completion_receipt,
    verify_latest_completion_receipt,
)


class _Function:
    def __init__(self, error_number=0):
        self.error_number = error_number
        self.argtypes = None
        self.restype = None

    def __call__(self, *_args):
        if self.error_number:
            ctypes.set_errno(self.error_number)
            return -1
        return 0


class _Library:
    def __init__(self, error_number=0):
        self.renameatx_np = _Function(error_number)


class CompletionReceiptTests(unittest.TestCase):
    TXN = "a" * 32
    APP = "b" * 64
    CONFIG = "c" * 64

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name)
        self.fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY)

    def tearDown(self):
        os.close(self.fd)
        self.temp.cleanup()

    def test_receipt_is_exact_owner_only_durable_schema_and_verifies_hashes(self):
        name = write_completion_receipt(self.fd, self.TXN, self.APP, self.CONFIG,
                                        completed_at="2026-07-11T01:02:03+00:00")
        completed = self.parent / "completed"
        self.assertEqual(0o700, completed.stat().st_mode & 0o777)
        self.assertEqual(0o600, (completed / name).stat().st_mode & 0o777)
        payload = json.loads((completed / name).read_text())
        self.assertEqual({"schema_version", "transaction_id", "transaction_state",
                          "cleanup_state", "app_tree_sha256", "config_sha256",
                          "completed_at", "result_classification"}, set(payload))
        self.assertNotIn("/", (completed / name).read_text())
        result = verify_latest_completion_receipt(
            self.fd, self.APP, self.CONFIG, now="2026-07-11T01:03:03+00:00")
        self.assertEqual(self.TXN, result["transaction_id"])

    def test_absent_config_marker_is_supported(self):
        write_completion_receipt(self.fd, self.TXN, self.APP, "absent",
                                 completed_at="2026-07-11T01:02:03+00:00")
        verify_latest_completion_receipt(
            self.fd, self.APP, "absent", now="2026-07-11T01:03:03+00:00")

    def test_tamper_stale_hash_mismatch_and_symlink_fail_closed(self):
        name = write_completion_receipt(self.fd, self.TXN, self.APP, self.CONFIG,
                                        completed_at="2026-07-11T01:02:03+00:00")
        with self.assertRaises(RecoveryError):
            verify_latest_completion_receipt(self.fd, "d" * 64, self.CONFIG,
                                             now="2026-07-11T01:03:03+00:00")
        with self.assertRaises(RecoveryError):
            verify_latest_completion_receipt(self.fd, self.APP, self.CONFIG,
                                             now="2026-07-13T01:03:03+00:00")
        receipt = self.parent / "completed" / name
        receipt.write_text(receipt.read_text().replace("COMMITTED", "TAMPERED"))
        with self.assertRaises(RecoveryError):
            verify_latest_completion_receipt(self.fd, self.APP, self.CONFIG,
                                             now="2026-07-11T01:03:03+00:00")

    def test_write_fault_before_rename_leaves_no_published_receipt(self):
        def fault(point):
            if point == "before_rename":
                raise RuntimeError("crash")
        with self.assertRaises(RuntimeError):
            write_completion_receipt(self.fd, self.TXN, self.APP, self.CONFIG,
                                     completed_at="2026-07-11T01:02:03+00:00", fault=fault)
        self.assertFalse(any(p.suffix == ".json" for p in (self.parent / "completed").iterdir()))
        write_completion_receipt(self.fd, self.TXN, self.APP, self.CONFIG,
                                 completed_at="2026-07-11T01:02:04+00:00")
        verify_latest_completion_receipt(self.fd, self.APP, self.CONFIG,
                                         now="2026-07-11T01:03:03+00:00")

    def test_crash_after_rename_leaves_verifiable_receipt(self):
        with self.assertRaises(RuntimeError):
            write_completion_receipt(
                self.fd, self.TXN, self.APP, self.CONFIG,
                completed_at="2026-07-11T01:02:03+00:00",
                fault=lambda point: (_ for _ in ()).throw(RuntimeError("crash"))
                if point == "after_rename" else None)
        verify_latest_completion_receipt(self.fd, self.APP, self.CONFIG,
                                         now="2026-07-11T01:03:03+00:00")

    def test_any_pending_transaction_cleanup_or_unknown_completed_entry_blocks_latest(self):
        write_completion_receipt(self.fd, self.TXN, self.APP, self.CONFIG,
                                 completed_at="2026-07-11T01:02:03+00:00")
        other = "d" * 32
        blockers = [
            self.parent / f"transaction.{other}",
            self.parent / f"cleanup.{other}.json",
            self.parent / "completed/.pending.tmp",
        ]
        for blocker in blockers:
            if blocker.name.startswith("transaction."):
                blocker.mkdir()
            else:
                blocker.write_text("pending")
            with self.subTest(blocker=blocker.name), self.assertRaises(RecoveryError):
                verify_latest_completion_receipt(
                    self.fd, self.APP, self.CONFIG, now="2026-07-11T01:03:03+00:00")
            if blocker.is_dir():
                blocker.rmdir()
            else:
                blocker.unlink()

    def test_receipt_symlink_wrong_mode_and_wrong_owner_fail_closed(self):
        name = write_completion_receipt(self.fd, self.TXN, self.APP, self.CONFIG,
                                        completed_at="2026-07-11T01:02:03+00:00")
        receipt = self.parent / "completed" / name
        body = receipt.read_bytes()
        receipt.chmod(0o640)
        with self.assertRaises(RecoveryError):
            verify_latest_completion_receipt(
                self.fd, self.APP, self.CONFIG, now="2026-07-11T01:03:03+00:00")
        receipt.unlink(); target = self.parent / "outside"; target.write_bytes(body)
        receipt.symlink_to(target)
        with self.assertRaises(RecoveryError):
            verify_latest_completion_receipt(
                self.fd, self.APP, self.CONFIG, now="2026-07-11T01:03:03+00:00")
        receipt.unlink(); receipt.write_bytes(body); receipt.chmod(0o600)
        with patch("macos.install_transaction.os.getuid", return_value=os.getuid() + 1), \
                self.assertRaises(RecoveryError):
            verify_latest_completion_receipt(
                self.fd, self.APP, self.CONFIG, now="2026-07-11T01:03:03+00:00")


class RenameAtxTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS SDK ABI test")
    def test_sdk_headers_confirm_renameatx_np_abi_and_flags(self):
        sdk = subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
                             check=True, capture_output=True, text=True).stdout.strip()
        source = r'''#include <stdio.h>
#include <sys/attr.h>
#include <sys/types.h>
#include <unistd.h>
_Static_assert(RENAME_SWAP == 0x00000002, "RENAME_SWAP mismatch");
_Static_assert(RENAME_EXCL == 0x00000004, "RENAME_EXCL mismatch");
typedef int (*expected_t)(int, const char *, int, const char *, unsigned int);
_Static_assert(__builtin_types_compatible_p(__typeof__(&renameatx_np), expected_t), "renameatx_np ABI mismatch");
int main(void) { return 0; }
'''
        subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "clang", "-isysroot", sdk,
                        "-x", "c", "-fsyntax-only", "-"], input=source, text=True,
                       check=True, capture_output=True)

    def test_constants_and_abi_are_exact(self):
        library = _Library()
        load_renameatx(library)
        self.assertEqual(0x2, RENAME_SWAP)
        self.assertEqual(0x4, RENAME_EXCL)
        self.assertEqual(
            [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
             ctypes.c_char_p, ctypes.c_uint],
            library.renameatx_np.argtypes,
        )
        self.assertIs(ctypes.c_int, library.renameatx_np.restype)

    def test_all_failures_are_classified_without_fallback(self):
        cases = ((errno.EEXIST, "collision"), (errno.ENOENT, "race"),
                 (errno.ENOTSUP, "unsupported"), (errno.EINVAL, "unsupported"),
                 (errno.EXDEV, "mount"), (errno.EIO, "syscall"))
        for number, message in cases:
            with self.subTest(number=number), patch("os.rename") as fallback:
                with self.assertRaisesRegex(InstallError, message):
                    rename_swap(3, b"old", 4, b"new", libc=_Library(number))
                fallback.assert_not_called()

    def test_absent_symbol_and_nul_are_fail_closed(self):
        with patch("os.rename") as fallback:
            with self.assertRaisesRegex(InstallError, "利用できません"):
                load_renameatx(object())
            with self.assertRaisesRegex(ValueError, "NUL"):
                rename_exclusive(3, b"bad\0name", 3, b"new", libc=_Library())
        fallback.assert_not_called()


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)

    def tearDown(self):
        os.close(self.fd)
        self.tmp.cleanup()

    def test_state_and_pointer_are_immutable_and_orphan_state_is_resumed(self):
        journal = Journal(self.fd, "a" * 32, rename=lambda d1, a, d2, b: os.rename(a, b, src_dir_fd=d1, dst_dir_fd=d2))
        first = journal.append("TXN_PREPARED", {"value": 1})
        second = journal.write_state_only("PREPARED", {"value": 2})
        self.assertEqual(1, first["generation"])
        self.assertFalse((self.root / "current.2").exists())
        loaded = journal.recover_head()
        self.assertEqual(second, loaded)
        self.assertTrue((self.root / "current.2").exists())

    def test_corrupt_pointer_stops_without_mutation(self):
        journal = Journal(self.fd, "b" * 32, rename=lambda d1, a, d2, b: os.rename(a, b, src_dir_fd=d1, dst_dir_fd=d2))
        journal.append("TXN_PREPARED", {})
        pointer = self.root / "current.1"
        pointer.write_text("{}")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.assertRaisesRegex(RecoveryError, "隔離"):
            journal.recover_head()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})


class TreeDigestTests(unittest.TestCase):
    def test_tree_digest_survives_verified_copy_to_new_inodes(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            target = Path(root) / "target"
            source.mkdir(mode=0o700)
            (source / "file").write_bytes(b"same content")
            (source / "file").chmod(0o600)
            target.mkdir(mode=0o700)
            allowed_files, allowed_dirs = {"file"}, {"."}
            first = create_bundle_manifest(source, os.getuid(), allowed_files=allowed_files,
                                           allowed_dirs=allowed_dirs)
            fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
            try:
                copy_manifest_tree(first, source, fd)
            finally:
                os.close(fd)
            second = create_bundle_manifest(target, os.getuid(), allowed_files=allowed_files,
                                            allowed_dirs=allowed_dirs)
            self.assertNotEqual(first.files[0].identity.ino, second.files[0].identity.ino)
            self.assertEqual(first.tree_sha256, second.tree_sha256)


class RecoveryMatrixTests(unittest.TestCase):
    def test_all_fifteen_cells_have_fixed_actions_and_no_mixed_result(self):
        expected = {
            ("a", "absent"): ("old", "absent"), ("a", "old"): ("old", "old"), ("a", "new"): ("new", "new"),
            ("b", "absent"): ("old", "absent"), ("b", "old"): ("old", "old"), ("b", "new"): ("new", "new"),
            ("c", "absent"): ("old", "absent"), ("c", "old"): ("old", "old"), ("c", "new"): ("new", "new"),
            ("d", "absent"): (None, "absent"), ("d", "old"): (None, "old"), ("d", "new"): ("new", "new"),
            ("e", "absent"): (None, "absent"), ("e", "old"): (None, "old"), ("e", "new"): ("new", "new"),
        }
        for cell, result in expected.items():
            with self.subTest(cell=cell):
                self.assertEqual(result, classify_config_state(*cell).result)
                self.assertIsInstance(recovery_actions(*cell), tuple)

    def test_each_recovery_mutation_resumes_after_mutation_fsync_crash(self):
        fixtures = {
            "OLD_TO_BACKUP": ({"config.json": b"old", "temp": b"new"}, {"backup": b"old", "temp": b"new"}),
            "PUBLISH_TEMP": ({"temp": b"new"}, {"config.json": b"new"}),
            "NEW_TO_TRASH": ({"config.json": b"new"}, {"trash": b"new"}),
            "RESTORE_BACKUP": ({"backup": b"old"}, {"config.json": b"old"}),
            "DROP_TEMP": ({"temp": b"new"}, {}),
            "DROP_BACKUP": ({"backup": b"old"}, {}),
            "DROP_TRASH": ({"trash": b"new"}, {}),
        }
        for action, (before, after) in fixtures.items():
            with self.subTest(action=action), tempfile.TemporaryDirectory() as root:
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    for name, body in before.items():
                        (Path(root) / name).write_bytes(body)
                    with self.assertRaisesRegex(RuntimeError, "fault"):
                        execute_recovery_action(fd, action, crash_after_mutation=True)
                    execute_recovery_action(fd, action)
                    actual = {p.name: p.read_bytes() for p in Path(root).iterdir()}
                    self.assertEqual(after, actual)
                finally:
                    os.close(fd)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "Xserverメール通知管理.app"
        (self.bundle / "Contents").mkdir(parents=True)
        (self.bundle / "Contents/Info.plist").write_bytes(b"plist")
        (self.bundle / "Contents/Info.plist").chmod(0o600)
        self.bundle.chmod(0o700)
        (self.bundle / "Contents").chmod(0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_detects_inode_replacement_before_copy(self):
        manifest = create_bundle_manifest(self.bundle, os.getuid(),
                                          allowed_files={"Contents/Info.plist"},
                                          allowed_dirs={".", "Contents"})
        target = self.bundle / "Contents/Info.plist"
        target.unlink()
        target.write_bytes(b"plist")
        target.chmod(0o600)
        destination = self.root / "stage"
        destination.mkdir(mode=0o700)
        fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaisesRegex(InstallError, "検証後に変更"):
                copy_manifest_tree(manifest, self.bundle, fd)
        finally:
            os.close(fd)

    def test_manifest_rejects_hardlinks_and_unknown_entries(self):
        os.link(self.bundle / "Contents/Info.plist", self.bundle / "Contents/extra")
        with self.assertRaises(InstallError):
            create_bundle_manifest(self.bundle, os.getuid(),
                                   allowed_files={"Contents/Info.plist"},
                                   allowed_dirs={".", "Contents"})


class CleanupReceiptTests(unittest.TestCase):
    TXN = "c" * 32

    def _fixture(self):
        temp = tempfile.TemporaryDirectory()
        parent = Path(temp.name)
        transaction = parent / f"transaction.{self.TXN}"
        (transaction / "nested").mkdir(parents=True, mode=0o700)
        (transaction / "state.1.json").write_bytes(b"state")
        (transaction / "nested" / "payload").write_bytes(b"payload")
        for path in (transaction / "state.1.json", transaction / "nested" / "payload"):
            path.chmod(0o600)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        transaction_fd = os.open(transaction, os.O_RDONLY | os.O_DIRECTORY)
        try:
            expected_root, expected_entries = capture_cleanup_inventory(transaction_fd)
        finally:
            os.close(transaction_fd)
        receipt = create_cleanup_receipt(fd, transaction.name, self.TXN,
                                         expected_root, expected_entries)
        return temp, parent, fd, receipt

    def test_receipt_is_durable_owner_only_and_cleanup_removes_it_last(self):
        temp, parent, fd, receipt = self._fixture()
        events = []
        try:
            self.assertEqual(0o600, os.stat(receipt, dir_fd=fd).st_mode & 0o777)
            resume_cleanup(fd, receipt, fault=lambda kind, path: events.append((kind, path)))
            self.assertEqual([], list(parent.iterdir()))
            self.assertEqual(
                [("file", "nested/payload"), ("dir", "nested"),
                 ("file", "state.1.json"), ("transaction", f"transaction.{self.TXN}")],
                events,
            )
        finally:
            os.close(fd)
            temp.cleanup()

    def test_every_unlink_rmdir_and_transaction_rmdir_fault_is_resumable(self):
        # There are two file unlinks, one nested rmdir and the transaction rmdir.
        for failure_index in range(4):
            with self.subTest(failure_index=failure_index):
                temp, parent, fd, receipt = self._fixture()
                calls = 0

                def fault(_kind, _path):
                    nonlocal calls
                    if calls == failure_index:
                        calls += 1
                        raise RuntimeError("injected cleanup crash")
                    calls += 1

                try:
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        resume_cleanup(fd, receipt, fault=fault)
                    self.assertTrue((parent / receipt).exists())
                    resume_cleanup(fd, receipt)
                    self.assertEqual([], list(parent.iterdir()))
                finally:
                    os.close(fd)
                    temp.cleanup()

    def test_unknown_or_identity_replaced_entry_stops_before_any_deletion(self):
        for mutation in ("unknown", "replace"):
            with self.subTest(mutation=mutation):
                temp, parent, fd, receipt = self._fixture()
                transaction = parent / f"transaction.{self.TXN}"
                try:
                    if mutation == "unknown":
                        (transaction / "unknown").write_bytes(b"surprise")
                    else:
                        target = transaction / "state.1.json"
                        target.unlink()
                        target.write_bytes(b"state")
                    before = sorted(path.relative_to(transaction).as_posix()
                                    for path in transaction.rglob("*"))
                    with self.assertRaisesRegex(RecoveryError, "cleanup対象"):
                        resume_cleanup(fd, receipt)
                    self.assertEqual(before, sorted(path.relative_to(transaction).as_posix()
                                                    for path in transaction.rglob("*")))
                    self.assertTrue((parent / receipt).exists())
                finally:
                    os.close(fd)
                    temp.cleanup()
