"""Production orchestration for a private, versioned Xserver release."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

try:
    from manager.release_deployer import build_manifest, build_stable_manifest
    from manager.remote_validator import RemoteValidationError
except ModuleNotFoundError:
    from release_deployer import build_manifest, build_stable_manifest
    from remote_validator import RemoteValidationError


class ReleaseWorkflowError(RuntimeError):
    pass


class ReleaseWorkflow:
    ENTRYPOINT = "bin/mail-to-lineworks.php"
    PRIVATE_ROOT = "/private/xserver-mail-lineworks"
    WRAPPER_SOURCE = b"<?php\nrequire __DIR__ . '/mail-forward-command.php';\n"
    # These values are part of the signed manager code.  They intentionally do
    # not derive trust from the bootstrap files being read.
    LEGACY_HELPER_SIZE = 69217
    LEGACY_HELPER_SHA256 = ("b4b5587789c28af5" + "5c61420196637dc7"
                            + "3a78701e1ba93f79" + "b9f81b91d6456387")
    LEGACY_MANIFEST_SIZE = 83849
    LEGACY_MANIFEST_SHA256 = ("4adf28efee6d6148" + "ff1394173c4b37fb"
                              + "a37b5a25cf803636" + "6cda4a6ad7158362")
    LEGACY_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "fixed-runtime/legacy-manifest.json"
    LEGACY_MANIFEST_MODE = 0o600 if Path(__file__).resolve().parent.name == "manager" \
        and Path(__file__).resolve().parents[1].name == "Resources" else 0o644
    CURRENT_GENERATION_MANIFEST_SIZE = 84003
    CURRENT_GENERATION_MANIFEST_SHA256 = ("7a07310945682c0f" + "c35b7b77260cbc2d"
                                          + "dfd6a3f48ae5885e" + "55bf7247292f5adb")
    CURRENT_GENERATION_MANIFEST_PATH = (Path(__file__).resolve().parents[1]
                                        / "fixed-runtime/generation-b9fd468-manifest.json")
    CURRENT_GENERATION_MANIFEST_MODE = LEGACY_MANIFEST_MODE
    FIXED_MIGRATION_ORDER = (
        "src/ReleaseValidator.php",
        "bootstrap/validate-release.php",
        "bootstrap/mail-forward-command.php",
    )

    def __init__(self, deployer, filesystem_home: str, config_path: str):
        self.deployer = deployer
        self.filesystem_home = filesystem_home.rstrip("/")
        self.config_path = config_path

    @staticmethod
    def _release_id(value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"release-[A-Za-z0-9_-]{1,96}", value) is None:
            raise ReleaseWorkflowError("リリースIDが不正です。")
        return value

    @staticmethod
    def _copy_plain_tree(source: Path, destination: Path) -> None:
        source_info = os.lstat(source)
        if (not source.is_absolute() or not stat.S_ISDIR(source_info.st_mode)
                or stat.S_ISLNK(source_info.st_mode)):
            raise ReleaseWorkflowError("リリース元を確認できません。")
        destination.mkdir()
        entry = source / "bin/mail-to-lineworks.php"
        if not entry.is_file() or entry.is_symlink():
            raise ReleaseWorkflowError("release entrypointが不足しています。")
        (destination / "bin").mkdir()
        shutil.copyfile(entry, destination / "bin/mail-to-lineworks.php")
        for name in ("src", "vendor"):
            tree = source / name
            if not tree.is_dir() or tree.is_symlink():
                raise ReleaseWorkflowError("release依存ファイルが不足しています。")
            shutil.copytree(tree, destination / name, symlinks=True)
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise ReleaseWorkflowError("リリースにシンボリックリンクは使用できません。")

    def _prepare(self, source: Path, destination: Path) -> tuple[list[dict], bytes]:
        self._copy_plain_tree(source, destination)
        for path in destination.rglob("*"):
            path.chmod(0o700 if path.is_dir() or path.relative_to(destination).as_posix() == self.ENTRYPOINT else 0o600)
        manifest_path = destination / "release-manifest.json"
        if manifest_path.exists() or manifest_path.is_symlink():
            raise ReleaseWorkflowError("予約済みmanifest名が含まれています。")
        initial = build_manifest(destination)
        stable = build_stable_manifest(
            initial, self.ENTRYPOINT, preload_paths=[], source_root=destination,
        )
        stable_bytes = (json.dumps(stable, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n").encode("utf-8")
        manifest_path.write_bytes(stable_bytes)
        manifest_path.chmod(0o600)
        return build_manifest(destination), stable_bytes

    def provision_fixed_runtime(self, source_root: Path) -> None:
        """Provision validator dependencies and the immutable bootstrap before SSH use."""
        source_root = Path(source_root)
        files = {
            "bootstrap/mail-forward-command.php": source_root / "bin/stable-mail-entrypoint.php",
            "bootstrap/mail-forward-command-701.php": source_root / "bin/mail-forward-command-701.php",
            "bootstrap/validate-release.php": source_root / "bin/validate-release.php",
            "bootstrap/manage-private-config.php": source_root / "bin/manage-private-config.php",
            "src/ReleaseValidator.php": source_root / "src/ReleaseValidator.php",
        }
        vendor = source_root / "vendor"
        if not vendor.is_dir():
            raise ReleaseWorkflowError("validator依存ファイルが不足しています。")
        for source in files.values():
            try:
                info = os.lstat(source)
            except OSError as error:
                raise ReleaseWorkflowError("validator依存ファイルが不足しています。") from error
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ReleaseWorkflowError("validator依存ファイルが不正です。")
        wrapper_source = files["bootstrap/mail-forward-command-701.php"]
        try:
            if wrapper_source.read_bytes() != self.WRAPPER_SOURCE:
                raise ReleaseWorkflowError("固定wrapper sourceが不正です。")
        except OSError as error:
            raise ReleaseWorkflowError("固定wrapper sourceが不正です。") from error
        with tempfile.TemporaryDirectory(prefix="xserver-fixed-") as temporary:
            root = Path(temporary)
            for relative, source in files.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative == "bootstrap/mail-forward-command-701.php":
                    target.write_bytes(self.WRAPPER_SOURCE)
                else:
                    shutil.copyfile(source, target)
                target.chmod(0o701 if relative == "bootstrap/mail-forward-command-701.php"
                             else 0o700 if relative.startswith("bootstrap/") else 0o600)
            shutil.copytree(vendor, root / "vendor", symlinks=True)
            for path in (root / "vendor").rglob("*"):
                if path.is_symlink():
                    raise ReleaseWorkflowError("validator依存ファイルが不正です。")
                path.chmod(0o700 if path.is_dir() else 0o600)

            expected = {}
            for path in root.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    expected[self.PRIVATE_ROOT + "/" + relative] = (
                        path.read_bytes(), "701" if relative == "bootstrap/mail-forward-command-701.php"
                        else "700" if relative.startswith("bootstrap/") else "600"
                    )
            ssh_entries = {}
            for path in root.rglob("*"):
                relative = path.relative_to(root).as_posix()
                if path.is_dir():
                    ssh_entries[relative] = {"type": "directory", "mode": 0o700,
                                             "size": 0, "sha256": None}
                else:
                    body = path.read_bytes()
                    ssh_entries[relative] = {
                        "type": "file",
                        "mode": 0o701 if relative == "bootstrap/mail-forward-command-701.php"
                        else 0o700 if relative.startswith("bootstrap/") else 0o600,
                        "size": len(body), "sha256": __import__("hashlib").sha256(body).hexdigest(),
                    }
            filesystem_root = self.filesystem_home + self.PRIVATE_ROOT
            expected_hosts = self.deployer.validation_context["expected_hosts"]
            try:
                state = self.deployer.remote_validator.inspect_fixed_runtime(
                    filesystem_root, ssh_entries, expected_hosts=expected_hosts
                )
            except RemoteValidationError:
                # A strict mismatch is not the inspector's valid-subset PARTIAL
                # state.  In particular, pinned migration prefixes already have
                # the helper and must bypass the missing-helper compatibility
                # branch, whose expected tree deliberately omits it.
                self._migrate_fixed_runtime(ssh_entries, expected,
                                            expected_hosts=expected_hosts)
                state = "EXACT"
            helper_relative = "bootstrap/manage-private-config.php"
            helper_remote = self.PRIVATE_ROOT + "/" + helper_relative
            if state == "PARTIAL":
                legacy_ssh_entries = {
                    path: item for path, item in ssh_entries.items()
                    if path != helper_relative
                }
                legacy_state = self.deployer.remote_validator.inspect_fixed_runtime(
                    filesystem_root, legacy_ssh_entries, expected_hosts=expected_hosts
                )
                if legacy_state == "EXACT":
                    legacy_expected = {
                        path: item for path, item in expected.items()
                        if path != helper_remote
                    }
                    try:
                        self.deployer.ftps.verify_private_files(legacy_expected)
                        helper_body, helper_mode = expected[helper_remote]
                        readback = self.deployer.ftps.replace_bytes_atomic(
                            helper_remote, helper_body, mode=helper_mode
                        )
                        if readback != helper_body:
                            raise RuntimeError("helper readback mismatch")
                        self.deployer.ftps.verify_private_files({
                            helper_remote: (helper_body, helper_mode)
                        })
                    except RuntimeError as error:
                        raise ReleaseWorkflowError("legacy固定runtimeの更新に失敗しました。") from error
                    if self.deployer.remote_validator.inspect_fixed_runtime(
                        filesystem_root, ssh_entries, expected_hosts=expected_hosts
                    ) != "EXACT":
                        raise ReleaseWorkflowError("legacy固定runtimeのSSH検証に失敗しました。")
                    state = "EXACT"
            if state == "PARTIAL":
                self._migrate_fixed_runtime(ssh_entries, expected,
                                            expected_hosts=expected_hosts)
                state = "EXACT"
            try:
                ftps_present = self.deployer.ftps.verify_private_files(
                    expected, allow_all_missing=True
                )
            except RuntimeError as error:
                raise ReleaseWorkflowError("既存の固定runtimeが異なるため停止しました。") from error
            if state == "PARTIAL":
                raise ReleaseWorkflowError("固定runtimeが部分状態のため停止しました。")
            if state == "ABSENT" and ftps_present:
                raise ReleaseWorkflowError("固定runtimeのSSH/FTPS状態が一致しません。")
            if state == "EXACT" and not ftps_present:
                raise ReleaseWorkflowError("固定runtimeのSSH/FTPS状態が一致しません。")
            if not ftps_present:
                generation = hashlib.sha256(json.dumps(
                    ssh_entries, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest()[:32]
                staging_name = ".fixed-staging-" + generation
                staging_root = self.PRIVATE_ROOT.rsplit("/", 1)[0] + "/" + staging_name
                staging_filesystem = self.filesystem_home + staging_root
                staging_state = self.deployer.remote_validator.inspect_fixed_runtime(
                    staging_filesystem, ssh_entries, expected_hosts=expected_hosts
                )
                if staging_state == "PARTIAL":
                    self.deployer.ftps.delete_exact_tree(
                        staging_root,
                        [path for path, item in ssh_entries.items() if item["type"] == "file"],
                        [path for path, item in ssh_entries.items() if item["type"] == "directory"],
                    )
                elif staging_state != "ABSENT":
                    raise ReleaseWorkflowError("staging generationが既に存在します。")
                self.deployer.ftps.deploy_release(root, staging_root)
                staging_expected = {
                    staging_root + remote.removeprefix(self.PRIVATE_ROOT): value
                    for remote, value in expected.items()
                }
                try:
                    self.deployer.ftps.verify_private_files(staging_expected)
                except RuntimeError as error:
                    raise ReleaseWorkflowError("staging readbackに失敗しました。") from error
                if self.deployer.remote_validator.inspect_fixed_runtime(
                    staging_filesystem, ssh_entries, expected_hosts=expected_hosts
                ) != "EXACT":
                    raise ReleaseWorkflowError("staging SSH検証に失敗しました。")
                self.deployer.ftps.publish_directory(staging_root, self.PRIVATE_ROOT)
            try:
                self.deployer.ftps.verify_private_files(expected)
            except RuntimeError as error:
                raise ReleaseWorkflowError("固定runtimeのreadbackに失敗しました。") from error
            if self.deployer.remote_validator.inspect_fixed_runtime(
                filesystem_root, ssh_entries, expected_hosts=expected_hosts
            ) != "EXACT":
                raise ReleaseWorkflowError("固定runtimeの公開後検証に失敗しました。")

    @staticmethod
    def _validate_fixed_manifest(manifest_body: bytes) -> dict:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        try:
            manifest = json.loads(manifest_body.decode("utf-8"), object_pairs_hook=unique)
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ReleaseWorkflowError("bootstrap manifestが不正です。") from error
        if (type(manifest) is not dict or set(manifest) != {"schema_version", "entries"}
                or type(manifest["schema_version"]) is not int
                or manifest["schema_version"] != 1 or type(manifest["entries"]) is not dict
                or not manifest["entries"]):
            raise ReleaseWorkflowError("bootstrap manifestが不正です。")
        entries = manifest["entries"]
        for relative, item in entries.items():
            if (type(relative) is not str or not relative or relative.startswith("/")
                    or ".." in relative.split("/") or "public_html" in relative.casefold()
                    or type(item) is not dict
                    or set(item) != {"type", "mode", "size", "sha256"}
                    or item["type"] not in {"file", "directory"}
                    or type(item["mode"]) is not int or item["mode"] not in {0o600, 0o700, 0o701}
                    or type(item["size"]) is not int or item["size"] < 0
                    or (item["type"] == "directory"
                        and (item["mode"] != 0o700 or item["size"] != 0 or item["sha256"] is not None))
                    or (item["type"] == "file" and
                        (type(item["sha256"]) is not str
                         or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None
                         or (item["mode"] == 0o701
                             and relative != "bootstrap/mail-forward-command-701.php")))):
                raise ReleaseWorkflowError("bootstrap manifestが不正です。")
        return entries

    @staticmethod
    def _file_hash_record(item: dict) -> dict:
        return {"size": item["size"], "sha256": item["sha256"],
                "mode": format(item["mode"], "03o")}

    def _migrate_fixed_runtime(self, new_entries: dict, target_expected: dict,
                               *, expected_hosts: list[str]) -> None:
        """Advance only the pinned legacy tree through the three reviewed prefixes."""
        manifest_body = self._read_pinned_asset(
            Path(self.LEGACY_MANIFEST_PATH), expected_mode=self.LEGACY_MANIFEST_MODE,
            expected_size=self.LEGACY_MANIFEST_SIZE,
            expected_sha256=self.LEGACY_MANIFEST_SHA256,
        )
        legacy_entries = self._validate_fixed_manifest(manifest_body)
        helper_relative = "bootstrap/manage-private-config.php"
        if helper_relative in legacy_entries or helper_relative not in new_entries:
            raise ReleaseWorkflowError("legacy固定runtime manifestが不正です。")
        helper = new_entries[helper_relative]
        if (helper["type"] != "file" or helper["size"] != self.LEGACY_HELPER_SIZE
                or not hmac.compare_digest(helper["sha256"], self.LEGACY_HELPER_SHA256)
                or helper["mode"] != 0o700):
            raise ReleaseWorkflowError("固定helperがpinned generationと異なります。")
        current_manifest_body = self._read_pinned_asset(
            Path(self.CURRENT_GENERATION_MANIFEST_PATH),
            expected_mode=self.CURRENT_GENERATION_MANIFEST_MODE,
            expected_size=self.CURRENT_GENERATION_MANIFEST_SIZE,
            expected_sha256=self.CURRENT_GENERATION_MANIFEST_SHA256,
        )
        current_generation = self._validate_fixed_manifest(current_manifest_body)
        old_helper = current_generation.get(helper_relative)
        if (not isinstance(old_helper, dict) or old_helper.get("type") != "file"
                or old_helper.get("mode") != 0o700):
            raise ReleaseWorkflowError("current generation manifestが不正です。")
        expected_current_generation = {
            relative: dict(item) for relative, item in new_entries.items()
        }
        expected_current_generation[helper_relative] = dict(old_helper)
        if current_generation != expected_current_generation:
            raise ReleaseWorkflowError("current generation manifestがtargetと一致しません。")
        legacy_full = dict(legacy_entries)
        legacy_full[helper_relative] = dict(helper)
        legacy_full_old_helper = dict(legacy_entries)
        legacy_full_old_helper[helper_relative] = dict(old_helper)
        if set(legacy_full) != set(new_entries):
            raise ReleaseWorkflowError("legacy固定runtimeの構成が異なります。")
        for relative in set(new_entries) - set(self.FIXED_MIGRATION_ORDER):
            if legacy_full[relative] != new_entries[relative]:
                raise ReleaseWorkflowError("許可されていない固定runtime変更です。")
        for relative in self.FIXED_MIGRATION_ORDER:
            if (relative not in legacy_full or legacy_full[relative]["type"] != "file"
                    or new_entries[relative]["type"] != "file"):
                raise ReleaseWorkflowError("migration対象が不足しています。")

        prefix_families = []
        for family, initial in (("old", legacy_full_old_helper), ("new", legacy_full)):
            family_prefixes = []
            current = {relative: dict(item) for relative, item in initial.items()}
            family_prefixes.append(current)
            for relative in self.FIXED_MIGRATION_ORDER:
                current = {path: dict(item) for path, item in current.items()}
                current[relative] = dict(new_entries[relative])
                family_prefixes.append(current)
            prefix_families.append((family, family_prefixes))
        filesystem_root = self.filesystem_home + self.PRIVATE_ROOT
        def inspect_candidate(root, entries):
            try:
                return self.deployer.remote_validator.inspect_fixed_runtime(
                    root, entries, expected_hosts=expected_hosts)
            except RemoteValidationError:
                return None

        exact = [(family, index, prefixes) for family, prefixes in prefix_families
                 for index, entries in enumerate(prefixes)
                 if inspect_candidate(filesystem_root, entries) == "EXACT"]
        if len(exact) != 1 or (exact[0][0] == "new"
                              and exact[0][1] == len(self.FIXED_MIGRATION_ORDER)):
            raise ReleaseWorkflowError("固定runtimeが許可されたprefixではありません。")
        family, prefix, prefixes = exact[0]
        if family == "old" and prefix == len(self.FIXED_MIGRATION_ORDER):
            self._migrate_fixed_generation(
                current_generation, new_entries,
                target_expected=target_expected, expected_hosts=expected_hosts,
            )
            return
        self._run_fixed_migration_transaction(
            prefixes, prefix, self.FIXED_MIGRATION_ORDER,
            generation=self.LEGACY_MANIFEST_SHA256[:32],
            target_expected=target_expected, expected_hosts=expected_hosts,
            label="migration",
        )
        if family == "old":
            self._migrate_fixed_generation(
                current_generation, new_entries,
                target_expected=target_expected, expected_hosts=expected_hosts,
            )

    def _migrate_fixed_generation(self, current_manifest: dict, target_entries: dict,
                                  migration_order=("bootstrap/manage-private-config.php",),
                                  *, target_expected: dict, expected_hosts: list[str]) -> None:
        """Atomically advance one independently pinned fixed-runtime generation."""
        if (type(current_manifest) is not dict or type(target_entries) is not dict
                or type(migration_order) is not tuple or not migration_order
                or any(type(path) is not str for path in migration_order)
                or set(current_manifest) != set(target_entries)):
            raise ReleaseWorkflowError("fixed generation migrationが不正です。")
        for relative in set(target_entries) - set(migration_order):
            if current_manifest[relative] != target_entries[relative]:
                raise ReleaseWorkflowError("許可されていない固定runtime変更です。")
        for relative in migration_order:
            if (relative not in current_manifest
                    or current_manifest[relative].get("type") != "file"
                    or target_entries[relative].get("type") != "file"):
                raise ReleaseWorkflowError("generation migration対象が不足しています。")

        prefixes = [{path: dict(item) for path, item in current_manifest.items()}]
        for relative in migration_order:
            next_prefix = {path: dict(item) for path, item in prefixes[-1].items()}
            next_prefix[relative] = dict(target_entries[relative])
            prefixes.append(next_prefix)
        filesystem_root = self.filesystem_home + self.PRIVATE_ROOT

        def inspect(root, entries):
            try:
                return self.deployer.remote_validator.inspect_fixed_runtime(
                    root, entries, expected_hosts=expected_hosts)
            except RemoteValidationError:
                return None

        exact = [index for index, entries in enumerate(prefixes)
                 if inspect(filesystem_root, entries) == "EXACT"]
        if len(exact) != 1:
            raise ReleaseWorkflowError("固定runtimeがgeneration prefixではありません。")
        prefix = exact[0]
        if prefix == len(migration_order):
            return
        self._run_fixed_migration_transaction(
            prefixes, prefix, migration_order,
            generation=self.CURRENT_GENERATION_MANIFEST_SHA256[:32],
            target_expected=target_expected, expected_hosts=expected_hosts,
            label="generation",
        )

    def _run_fixed_migration_transaction(self, prefixes: list[dict], prefix: int,
                                         migration_order: tuple[str, ...], *,
                                         generation: str, target_expected: dict,
                                         expected_hosts: list[str], label: str) -> None:
        """Run the shared backup/publish/replace transaction with exact prevalidation."""
        filesystem_root = self.filesystem_home + self.PRIVATE_ROOT
        backup_root = self.PRIVATE_ROOT.rsplit("/", 1)[0] + "/.fixed-backup-" + generation
        backup_filesystem = self.filesystem_home + backup_root
        directories = sorted({relative.rsplit("/", 1)[0] for relative in migration_order})
        backup_entries = {directory: {"type": "directory", "mode": 0o700,
                                      "size": 0, "sha256": None}
                          for directory in directories}
        for relative in migration_order:
            backup_entries[relative] = dict(prefixes[0][relative])

        def inspect(root, entries):
            try:
                return self.deployer.remote_validator.inspect_fixed_runtime(
                    root, entries, expected_hosts=expected_hosts)
            except RemoteValidationError:
                return None

        def inspect_details(root, entries):
            try:
                return self.deployer.remote_validator.inspect_fixed_runtime_details(
                    root, entries, expected_hosts=expected_hosts)
            except RemoteValidationError:
                return None

        def hash_records(root, entries):
            return {root + "/" + relative: self._file_hash_record(item)
                    for relative, item in entries.items() if item["type"] == "file"}

        def verify_exact(root, filesystem, entries, purpose):
            if inspect(filesystem, entries) != "EXACT":
                raise ReleaseWorkflowError(label + " " + purpose + "のSSH検証に失敗しました。")
            try:
                self.deployer.ftps.verify_private_file_hashes(hash_records(root, entries))
            except RuntimeError as error:
                raise ReleaseWorkflowError(
                    label + " " + purpose + "のFTPS検証に失敗しました。") from error

        def verify_absent(root, filesystem, entries, purpose):
            if inspect(filesystem, entries) != "ABSENT":
                raise ReleaseWorkflowError(label + " " + purpose + "が不正です。")
            try:
                for remote, item in hash_records(root, entries).items():
                    if self.deployer.ftps.read_optional_bytes(
                            remote, limit=min(item["size"] + 1, 65536)) is not None:
                        raise ReleaseWorkflowError(label + " " + purpose + "が不正です。")
            except ReleaseWorkflowError:
                raise
            except RuntimeError as error:
                raise ReleaseWorkflowError(
                    label + " " + purpose + "のFTPS検証に失敗しました。") from error

        def verify_live(index):
            verify_exact(self.PRIVATE_ROOT, filesystem_root, prefixes[index], "live prefix")

        def verify_backup():
            verify_exact(backup_root, backup_filesystem, backup_entries, "backup")

        backup_state = inspect(backup_filesystem, backup_entries)
        if prefix > 0 and backup_state != "EXACT":
            raise ReleaseWorkflowError(label + " backupを確認できません。")
        if prefix == 0 and backup_state == "ABSENT":
            verify_live(prefix)
            old_bytes = {}
            for relative in migration_order:
                item = prefixes[0][relative]
                remote = self.PRIVATE_ROOT + "/" + relative
                body = self.deployer.ftps.read_bytes(remote, limit=item["size"] + 1)
                if (len(body) != item["size"] or not hmac.compare_digest(
                        hashlib.sha256(body).hexdigest(), item["sha256"])):
                    raise ReleaseWorkflowError(label + " bytesを読み出せません。")
                old_bytes[relative] = body
            staging_root = (self.PRIVATE_ROOT.rsplit("/", 1)[0]
                            + "/.fixed-backup-staging-" + generation)
            staging_filesystem = self.filesystem_home + staging_root
            staging_state = inspect(staging_filesystem, backup_entries)
            if staging_state == "PARTIAL":
                staging_expected = hash_records(staging_root, backup_entries)
                first_details = inspect_details(staging_filesystem, backup_entries)
                if first_details is None or first_details["state"] != "PARTIAL":
                    raise ReleaseWorkflowError(label + " backup stagingが変更されました。")
                first_required = frozenset(
                    staging_root + "/" + relative
                    for relative in first_details["present_files"])
                try:
                    first_subset = self.deployer.ftps.verify_private_file_hash_subset(
                        staging_expected, first_required)
                except RuntimeError as error:
                    raise ReleaseWorkflowError(
                        label + " backup stagingのFTPS検証に失敗しました。") from error
                if first_subset != first_required:
                    raise ReleaseWorkflowError(label + " backup stagingが変更されました。")
                second_details = inspect_details(staging_filesystem, backup_entries)
                if (second_details is None or second_details["state"] != "PARTIAL"
                        or second_details["present_files"] != first_details["present_files"]):
                    raise ReleaseWorkflowError(label + " backup stagingが変更されました。")
                second_required = frozenset(
                    staging_root + "/" + relative
                    for relative in second_details["present_files"])
                try:
                    second_subset = self.deployer.ftps.verify_private_file_hash_subset(
                        staging_expected, second_required)
                except RuntimeError as error:
                    raise ReleaseWorkflowError(
                        label + " backup stagingのFTPS検証に失敗しました。") from error
                if second_subset != second_required:
                    raise ReleaseWorkflowError(label + " backup stagingが変更されました。")
                verify_absent(backup_root, backup_filesystem, backup_entries, "backup")
                verify_live(prefix)
                self.deployer.ftps.delete_exact_tree(staging_root, list(migration_order), directories)
                staging_state = "ABSENT"
            if staging_state not in {"ABSENT", "EXACT"}:
                raise ReleaseWorkflowError(label + " backup stagingが不正です。")
            if staging_state == "ABSENT":
                verify_absent(staging_root, staging_filesystem, backup_entries,
                              "backup staging")
                verify_absent(backup_root, backup_filesystem, backup_entries, "backup")
                verify_live(prefix)
                with tempfile.TemporaryDirectory(prefix="xserver-fixed-backup-") as temporary:
                    local = Path(temporary)
                    for relative, body in old_bytes.items():
                        target = local / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.parent.chmod(0o700)
                        target.write_bytes(body)
                        target.chmod(prefixes[0][relative]["mode"])
                    self.deployer.ftps.deploy_release(local, staging_root)
            verify_exact(staging_root, staging_filesystem, backup_entries, "backup staging")
            verify_absent(backup_root, backup_filesystem, backup_entries, "backup")
            verify_live(prefix)
            self.deployer.ftps.publish_directory(staging_root, backup_root)
            backup_state = inspect(backup_filesystem, backup_entries)
        if backup_state != "EXACT":
            raise ReleaseWorkflowError(label + " backupが不正です。")
        verify_backup()

        for index in range(prefix, len(migration_order)):
            verify_backup()
            verify_live(index)
            relative = migration_order[index]
            remote = self.PRIVATE_ROOT + "/" + relative
            old_item = prefixes[index][relative]
            new_item = prefixes[index + 1][relative]
            old_body = self.deployer.ftps.read_bytes(remote, limit=old_item["size"] + 1)
            backup_body = self.deployer.ftps.read_bytes(
                backup_root + "/" + relative, limit=old_item["size"] + 1)
            if old_body != backup_body:
                raise ReleaseWorkflowError(label + " prefixが変更されました。")
            source_body = target_expected[remote][0]
            try:
                result = self.deployer.ftps.replace_bytes_atomic(
                    remote, source_body, mode=format(new_item["mode"], "03o"))
            except RuntimeError:
                result = None
            readback = self.deployer.ftps.read_bytes(
                remote, limit=max(old_item["size"], new_item["size"]) + 1)
            if result is None and readback == old_body:
                verify_backup()
                verify_live(index)
                try:
                    result = self.deployer.ftps.replace_bytes_atomic(
                        remote, source_body, mode=format(new_item["mode"], "03o"))
                except RuntimeError:
                    result = None
                readback = self.deployer.ftps.read_bytes(
                    remote, limit=max(old_item["size"], new_item["size"]) + 1)
            if (result not in {None, source_body} or readback != source_body
                    or len(readback) != new_item["size"] or not hmac.compare_digest(
                        hashlib.sha256(readback).hexdigest(), new_item["sha256"])):
                raise ReleaseWorkflowError(label + " atomic置換を確認できません。")
            verify_exact(self.PRIVATE_ROOT, filesystem_root, prefixes[index + 1],
                         "published prefix")

    @staticmethod
    def _read_pinned_asset(path: Path, *, expected_mode: int, expected_size: int,
                           expected_sha256: str, expected_uid: int | None = None,
                           lstat_fn=os.lstat, open_fn=os.open, fstat_fn=os.fstat,
                           read_fn=os.read, close_fn=os.close) -> bytes:
        """Read one independently pinned local asset through one stable descriptor."""
        expected_uid = os.getuid() if expected_uid is None else expected_uid

        def snapshot(info):
            return (stat.S_IFMT(info.st_mode), info.st_uid, stat.S_IMODE(info.st_mode),
                    info.st_dev, info.st_ino, info.st_size)

        try:
            before = lstat_fn(path)
            if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                    or before.st_uid != expected_uid
                    or stat.S_IMODE(before.st_mode) != expected_mode
                    or before.st_size != expected_size):
                raise ReleaseWorkflowError("bootstrap資材が不正です。")
            fd = open_fn(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = fstat_fn(fd)
                if snapshot(opened) != snapshot(before):
                    raise ReleaseWorkflowError("bootstrap資材が変更されました。")
                chunks = []
                total = 0
                while total <= expected_size:
                    chunk = read_fn(fd, min(65536, expected_size + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                after = fstat_fn(fd)
                if snapshot(after) != snapshot(before):
                    raise ReleaseWorkflowError("bootstrap資材が変更されました。")
            finally:
                close_fn(fd)
        except ReleaseWorkflowError:
            raise
        except OSError as error:
            raise ReleaseWorkflowError("bootstrap資材を確認できません。") from error
        body = b"".join(chunks)
        if (len(body) != expected_size
                or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected_sha256)):
            raise ReleaseWorkflowError("bootstrap資材が不正です。")
        return body

    def provision_legacy_helper_assets(self, helper_path: Path, manifest_path: Path,
                                       *, expected_mode: int) -> bool:
        """Add only the missing helper to one exact legacy fixed runtime."""
        try:
            helper_body = self._read_pinned_asset(
                Path(helper_path), expected_mode=expected_mode,
                expected_size=self.LEGACY_HELPER_SIZE,
                expected_sha256=self.LEGACY_HELPER_SHA256,
            )
            manifest_body = self._read_pinned_asset(
                Path(manifest_path), expected_mode=expected_mode,
                expected_size=self.LEGACY_MANIFEST_SIZE,
                expected_sha256=self.LEGACY_MANIFEST_SHA256,
            )

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate")
                    result[key] = value
                return result
            manifest = json.loads(manifest_body.decode("utf-8"), object_pairs_hook=unique)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ReleaseWorkflowError("bootstrap資材を確認できません。") from error
        if (type(manifest) is not dict or set(manifest) != {"schema_version", "entries"}
                or type(manifest["schema_version"]) is not int
                or manifest["schema_version"] != 1 or type(manifest["entries"]) is not dict
                or not manifest["entries"]):
            raise ReleaseWorkflowError("bootstrap manifestが不正です。")
        legacy_entries = manifest["entries"]
        for relative, item in legacy_entries.items():
            if (type(relative) is not str or not relative or relative.startswith("/")
                    or ".." in relative.split("/") or relative == "bootstrap/manage-private-config.php"
                    or type(item) is not dict
                    or set(item) != {"type", "mode", "size", "sha256"}
                    or item["type"] not in {"file", "directory"}
                    or type(item["mode"]) is not int
                    or item["mode"] not in {0o600, 0o700, 0o701}
                    or type(item["size"]) is not int or item["size"] < 0
                    or (item["type"] == "directory" and (item["size"] != 0 or item["sha256"] is not None))
                    or (item["type"] == "file" and (type(item["sha256"]) is not str
                        or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None))):
                raise ReleaseWorkflowError("bootstrap manifestが不正です。")
            if ((item["type"] == "directory" and item["mode"] != 0o700)
                    or (item["type"] == "file" and item["mode"] == 0o701
                        and relative != "bootstrap/mail-forward-command-701.php")):
                raise ReleaseWorkflowError("bootstrap manifestが不正です。")
        helper_relative = "bootstrap/manage-private-config.php"
        helper_item = {"type": "file", "mode": 0o700, "size": len(helper_body),
                       "sha256": hashlib.sha256(helper_body).hexdigest()}
        full_entries = dict(legacy_entries)
        full_entries[helper_relative] = helper_item
        filesystem_root = self.filesystem_home + self.PRIVATE_ROOT
        expected_hosts = self.deployer.validation_context["expected_hosts"]
        state = self.deployer.remote_validator.inspect_fixed_runtime(
            filesystem_root, full_entries, expected_hosts=expected_hosts)
        if state == "EXACT":
            return False
        if state != "PARTIAL" or self.deployer.remote_validator.inspect_fixed_runtime(
                filesystem_root, legacy_entries, expected_hosts=expected_hosts) != "EXACT":
            raise ReleaseWorkflowError("helper欠落以外の固定runtime状態を拒否しました。")
        self.deployer.remote_validator.provision_fixed_helper(
            filesystem_root, helper_relative, helper_body,
            expected_sha256=helper_item["sha256"], mode=0o700,
            expected_hosts=expected_hosts,
        )
        if self.deployer.remote_validator.inspect_fixed_runtime(
                filesystem_root, full_entries, expected_hosts=expected_hosts) != "EXACT":
            raise ReleaseWorkflowError("helper追加後のSSH検証に失敗しました。")
        return True

    def stage(self, local_source: Path, release_id: str) -> dict:
        release_id = self._release_id(release_id)
        self.provision_fixed_runtime(Path(local_source))
        with tempfile.TemporaryDirectory(prefix="xserver-release-") as temporary:
            prepared = Path(temporary) / release_id
            manifest, stable_bytes = self._prepare(Path(local_source), prepared)
            ftps_root = f"{self.PRIVATE_ROOT}/releases/{release_id}"
            filesystem_root = f"{self.filesystem_home}{ftps_root}"
            result = self.deployer.stage_and_validate(
                prepared, ftps_root, validation_root=filesystem_root
            )
            return {
                "release_id": release_id,
                "release_path": filesystem_root,
                "manifest": manifest,
                "manifest_sha256": hashlib.sha256(stable_bytes).hexdigest(),
                "validation": result,
            }

    def switch(self, staged: dict) -> str:
        locator = {
            "schema_version": 1,
            "release_id": staged["release_id"],
            "release_path": staged["release_path"],
            "entrypoint": self.ENTRYPOINT,
            "manifest_sha256": staged["manifest_sha256"],
            "config_path": self.config_path,
        }
        return self.deployer.switch_locator(
            self.PRIVATE_ROOT + "/state/active-release.json", locator
        )
