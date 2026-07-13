"""Validate a private versioned release before atomically switching its locator."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from datetime import datetime, timedelta, timezone
from ftplib import error_perm
from pathlib import Path, PurePosixPath


MAX_READBACK = 64 * 1024 * 1024
LOCATOR_KEYS = frozenset({
    "schema_version", "release_id", "release_path", "entrypoint",
    "manifest_sha256", "config_path",
})


class ReleaseDeploymentError(RuntimeError):
    pass


def _private_absolute(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "//" in value:
        return False
    parts = value.split("/")[1:]
    return bool(parts) and not any(
        part in ("", ".", "..") or part.casefold() == "public_html" for part in parts
    )


def build_manifest(root: Path) -> dict:
    """Create a deterministic, non-following manifest for one local release."""
    root = Path(root)
    root_info = os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseDeploymentError("release root is invalid")
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().encode("utf-8")):
        info = os.lstat(path)
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseDeploymentError("release contains a symbolic link")
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "type": "directory", "mode": 0o700,
                            "size": 0, "sha256": None})
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseDeploymentError("release contains an unsupported file")
        data = path.read_bytes()
        mode = "0700" if stat.S_IMODE(info.st_mode) & 0o111 else "0600"
        entries.append({
            "type": "file",
            "path": relative, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "mode": int(mode, 8),
        })
    return sorted(entries, key=lambda item: item["path"].encode("utf-8"))


def _verified_php_source(source_root: Path, path: str, item: dict) -> bool:
    relative = PurePosixPath(path)
    if (relative.is_absolute() or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)):
        raise ReleaseDeploymentError("stable runtime path is invalid")
    source = Path(source_root).joinpath(*relative.parts)
    info = os.lstat(source)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ReleaseDeploymentError("stable runtime source is invalid")
    body = source.read_bytes()
    if len(body) != item.get("size") or hashlib.sha256(body).hexdigest() != item.get("sha256"):
        raise ReleaseDeploymentError("stable runtime source does not match manifest")
    return re.match(br"\A<\?php(?:\s|$)", body) is not None


def _build_stable_manifest(manifest: list[dict], entrypoint: str, *, preload_paths,
                           source_root: Path) -> dict:
    """Derive the bootstrap's execution manifest from the canonical release manifest."""
    paths = [item.get("path") for item in manifest if isinstance(item, dict)]
    if len(paths) != len(manifest) or len(set(paths)) != len(paths):
        raise ReleaseDeploymentError("stable manifest contains duplicate or invalid paths")
    files = {item["path"]: item for item in manifest if item.get("type") == "file"}
    entry = files.get(entrypoint)
    if entry is None or entry.get("mode") != 0o700:
        raise ReleaseDeploymentError("stable entrypoint is not in the release manifest")
    if not _verified_php_source(source_root, entrypoint, entry):
        raise ReleaseDeploymentError("stable entrypoint is not executable PHP source")
    preload_list = list(preload_paths)
    preload = set(preload_list)
    if (len(preload) != len(preload_list) or any(
            not isinstance(path, str) or path == entrypoint or not path.endswith(".php")
            or path not in files for path in preload)):
        raise ReleaseDeploymentError("stable preload is not in the release manifest")
    if any(not _verified_php_source(source_root, path, files[path]) for path in preload):
        raise ReleaseDeploymentError("stable preload is not executable PHP source")
    runtime = []
    for path in sorted(files, key=lambda value: value.encode("utf-8")):
        if path == entrypoint or not path.endswith(".php"):
            continue
        item = files[path]
        if item.get("mode") != 0o600:
            raise ReleaseDeploymentError("stable runtime mode is invalid")
        if not _verified_php_source(source_root, path, item):
            continue
        runtime.append({
            "path": path, "size": item["size"], "sha256": item["sha256"],
            "mode": 0o600, "preload": path in preload,
        })
    return {
        "schema_version": 1,
        "entrypoint": {
            "path": entrypoint, "size": entry["size"],
            "sha256": entry["sha256"], "mode": 0o700,
        },
        "runtime": runtime,
    }


def build_stable_manifest(manifest: list[dict], entrypoint: str, *, preload_paths,
                          source_root: Path) -> dict:
    try:
        return _build_stable_manifest(
            manifest, entrypoint, preload_paths=preload_paths, source_root=source_root,
        )
    except ReleaseDeploymentError as exc:
        raise ReleaseDeploymentError("stable manifest invalid") from exc
    except Exception as exc:
        raise ReleaseDeploymentError("stable manifest invalid") from exc


class ReleaseDeployer:
    def __init__(self, ftps, remote_validator, api=None, *, validation_context=None):
        self.ftps = ftps
        self.remote_validator = remote_validator
        self.api = api
        self.validation_context = dict(validation_context or {})

    @staticmethod
    def canonical_manifest(manifest: dict) -> bytes:
        return json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def stage_and_validate(self, local_dir: Path, remote_root: str, *, validation_root=None) -> dict:
        if not _private_absolute(remote_root):
            raise ReleaseDeploymentError("remote release path is invalid")
        manifest = build_manifest(local_dir)
        self.ftps.deploy_release(local_dir, remote_root)
        for item in manifest:
            if item["type"] != "file":
                continue
            remote_path = remote_root.rstrip("/") + "/" + item["path"]
            body = self.ftps.read_bytes(remote_path, limit=min(MAX_READBACK, item["size"] + 1))
            if len(body) != item["size"] or hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise ReleaseDeploymentError("FTPS readback did not match upload")
        validation_root = remote_root if validation_root is None else validation_root
        if not _private_absolute(validation_root):
            raise ReleaseDeploymentError("remote validation path is invalid")
        result = self.remote_validator.validate_release(
            manifest, remote_root=validation_root, **self.validation_context
        )
        required_pass = ("manifest", "php_cli", "autoload", "absolute_cli_dry_run", "public_root")
        if (not isinstance(result, dict) or any(result.get(key) != "PASS" for key in required_pass)
                or result.get("symlinks") != 0):
            raise ReleaseDeploymentError("remote release validation failed")
        return result

    def switch_locator(self, locator_path: str, locator: dict) -> str:
        if not _private_absolute(locator_path) or set(locator) != LOCATOR_KEYS:
            raise ReleaseDeploymentError("locator is invalid")
        for key in ("release_path", "config_path"):
            if not _private_absolute(locator[key]):
                raise ReleaseDeploymentError("locator is invalid")
        if (not isinstance(locator["schema_version"], int) or locator["schema_version"] != 1
                or not isinstance(locator["release_id"], str)
                or not isinstance(locator["entrypoint"], str)
                or PurePosixPath(locator["entrypoint"]).is_absolute()
                or any(part in ("", ".", "..") for part in locator["entrypoint"].split("/"))
                or not isinstance(locator["manifest_sha256"], str)
                or len(locator["manifest_sha256"]) != 64):
            raise ReleaseDeploymentError("locator is invalid")
        body = (json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        readback = self.ftps.replace_bytes_atomic(locator_path, body, mode="600")
        if readback != body:
            raise ReleaseDeploymentError("locator readback failed")
        return hashlib.sha256(body).hexdigest()

    @staticmethod
    def classify_convergence(locator_pair: str, filter_set: str, target_pair: str) -> str:
        table = {
            ("old", "old", "new"): "overlap-forward",
            ("old", "old+new", "new"): "overlap-rollback",
            ("new", "old+new", "new"): "cleanup",
            ("new", "new", "new"): "committed",
        }
        return table.get((locator_pair, filter_set, target_pair), "isolate")

    @staticmethod
    def _canonical_rule(item, *, require_id=True):
        if not isinstance(item, dict):
            raise ReleaseDeploymentError("invalid filter snapshot")
        keys = {"domain", "conditions", "action"}
        if not keys.issubset(item) or (require_id and not isinstance(item.get("id"), str)):
            raise ReleaseDeploymentError("invalid filter snapshot")
        result = {key: json.loads(json.dumps(item[key])) for key in sorted(keys)}
        if require_id:
            result["id"] = item["id"]
        return result

    @classmethod
    def _snapshot_bytes(cls, filters) -> bytes:
        normalized = [cls._canonical_rule(item) for item in filters]
        return json.dumps(
            sorted(normalized, key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")

    def _require_filter_set(self, expected):
        actual = self.api.snapshot_filters()
        if self._snapshot_bytes(actual) != self._snapshot_bytes(expected):
            raise ReleaseDeploymentError("unexpected filter set; migration isolated")
        return [self._canonical_rule(item) for item in actual]

    @staticmethod
    def _new_rule(rule):
        if not isinstance(rule, dict) or set(rule) != {"domain", "conditions", "action"}:
            raise ReleaseDeploymentError("new filter rule is invalid")
        return json.loads(json.dumps(rule))

    def _write_migration_state(self, path, payload):
        if not _private_absolute(path):
            raise ReleaseDeploymentError("migration state path is invalid")
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode()
        if self.ftps.replace_bytes_atomic(path, body, mode="600") != body:
            raise ReleaseDeploymentError("migration state readback failed")

    def _read_migration_state(self, path):
        if not _private_absolute(path):
            raise ReleaseDeploymentError("migration state path is invalid")
        try:
            body = self.ftps.read_bytes(path, limit=1024 * 1024)
        except (KeyError, FileNotFoundError):
            return None
        except error_perm as exc:
            if not str(exc).startswith("550"):
                raise
            return None
        try:
            value = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseDeploymentError("migration state is invalid") from exc
        if type(value) is not dict or set(value) != {
            "schema_version", "phase", "locator_pair", "old", "new",
            "window_started_at", "window_ended_at", "overlap_evidence_sha256",
            "authorized_at", "deadline_at",
        } or value["schema_version"] != 1 or value["phase"] not in {
            "prepared", "maintenance-authorized", "maintenance-window", "committed"
        } or value["locator_pair"] not in {"old", "new"} or type(value["old"]) is not list \
                or type(value["new"]) is not list \
                or (value["window_started_at"] is not None and not isinstance(value["window_started_at"], str)) \
                or (value["window_ended_at"] is not None and not isinstance(value["window_ended_at"], str)) \
                or (value["overlap_evidence_sha256"] is not None and not re.fullmatch(
                    r"[0-9a-f]{64}", value["overlap_evidence_sha256"])):
            raise ReleaseDeploymentError("migration state is invalid")
        parsed_times = {}
        for key in ("window_started_at", "window_ended_at", "authorized_at", "deadline_at"):
            timestamp = value[key]
            if timestamp is not None:
                try:
                    parsed = datetime.fromisoformat(timestamp)
                    if parsed.tzinfo is None:
                        raise ValueError
                except ValueError as exc:
                    raise ReleaseDeploymentError("migration state is invalid") from exc
                parsed_times[key] = parsed.astimezone(timezone.utc)
        phase = value["phase"]
        if phase == "prepared":
            if parsed_times or value["locator_pair"] != "old":
                raise ReleaseDeploymentError("migration state is invalid")
        else:
            if set(parsed_times) < {"authorized_at", "deadline_at"}:
                raise ReleaseDeploymentError("migration state is invalid")
            if parsed_times["deadline_at"] - parsed_times["authorized_at"] != timedelta(seconds=120):
                raise ReleaseDeploymentError("migration state is invalid")
            if phase == "maintenance-authorized":
                if set(parsed_times) != {"authorized_at", "deadline_at"} or value["locator_pair"] != "old":
                    raise ReleaseDeploymentError("migration state is invalid")
            elif phase == "maintenance-window":
                if set(parsed_times) != {"authorized_at", "deadline_at", "window_started_at"} \
                        or value["locator_pair"] != "old":
                    raise ReleaseDeploymentError("migration state is invalid")
            elif phase == "committed":
                if set(parsed_times) != {"authorized_at", "deadline_at", "window_started_at", "window_ended_at"} \
                        or value["locator_pair"] != "new":
                    raise ReleaseDeploymentError("migration state is invalid")
            if "window_started_at" in parsed_times and not (
                    parsed_times["authorized_at"] <= parsed_times["window_started_at"] <= parsed_times["deadline_at"]):
                raise ReleaseDeploymentError("migration state is invalid")
            if "window_ended_at" in parsed_times and not (
                    parsed_times["window_started_at"] <= parsed_times["window_ended_at"] <= parsed_times["deadline_at"]):
                raise ReleaseDeploymentError("migration state is invalid")
        return value

    def bootstrap_migrate(
        self, old_rules, new_rules, *, locator_pair="old", target_pair="new",
        state_path=None, overlap_evidence=None, max_downtime_seconds=None,
        confirm_maintenance=None, maintenance_marker_path=None,
        fault=None, output=None, switch_pair=None, clock=None,
    ):
        """Perform the sole API-mutating bootstrap migration with exact readbacks."""
        if self.api is None or not hasattr(self.api, "snapshot_filters"):
            raise ReleaseDeploymentError("XServer API is unavailable")
        if locator_pair not in ("old", "new") or target_pair != "new":
            raise ReleaseDeploymentError("unknown convergence state; migration isolated")
        old_rules = [self._canonical_rule(item) for item in old_rules]
        desired = [self._new_rule(item) for item in new_rules]
        actual = [self._canonical_rule(item) for item in self.api.snapshot_filters()]
        def body(item):
            return json.dumps({k: v for k, v in item.items() if k != "id"},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        old_bodies = Counter(body(item) for item in old_rules)
        new_bodies = Counter(body(item) for item in desired)
        actual_bodies = Counter(body(item) for item in actual)
        marker_path = maintenance_marker_path or state_path
        prior = self._read_migration_state(marker_path)
        maintenance_resume = (prior is not None and prior["phase"] in {
            "maintenance-authorized", "maintenance-window"
        })
        def contained(left, right):
            return all(count <= right[key] for key, count in left.items())
        if prior is not None and prior["phase"] == "maintenance-authorized" \
                and not any(key in actual_bodies for key in new_bodies) \
                and contained(actual_bodies, old_bodies):
            filter_state = "maintenance-authorized"
        elif prior is not None and prior["phase"] == "maintenance-window" \
                and not any(key in actual_bodies for key in old_bodies) \
                and contained(actual_bodies, new_bodies):
            filter_state = "maintenance"
        elif actual_bodies == old_bodies:
            filter_state = "old"
        elif actual_bodies == new_bodies:
            filter_state = "new"
        elif (locator_pair == "old" and contained(old_bodies, actual_bodies)
              and contained(actual_bodies, old_bodies + new_bodies)):
            filter_state = "old+new"
        elif (locator_pair == "new" and contained(new_bodies, actual_bodies)
              and contained(actual_bodies, old_bodies + new_bodies)):
            filter_state = "old+new"
        else:
            raise ReleaseDeploymentError("unexpected filter set; migration isolated")
        convergence = ("maintenance-resume" if filter_state in {"maintenance", "maintenance-authorized"} else
                       self.classify_convergence(locator_pair, filter_state, target_pair))
        if convergence == "isolate":
            raise ReleaseDeploymentError("unknown convergence state; migration isolated")
        expected = actual
        if filter_state == "old" and self._snapshot_bytes(expected) != self._snapshot_bytes(old_rules):
            raise ReleaseDeploymentError("unexpected filter set; migration isolated")
        fault = fault or (lambda _point: None)
        output = output or (lambda _message: None)
        clock = clock or (lambda: datetime.now(timezone.utc))
        def now():
            value = clock()
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ReleaseDeploymentError("maintenance clock is invalid")
            return value.astimezone(timezone.utc)
        def persist_state(payload):
            fault("before_state_write")
            self._write_migration_state(marker_path, payload)
            fault("after_state_write")
        def perform_switch():
            ensure_deadline()
            fault("before_switch")
            switch_pair()
            fault("after_switch")
        if not expected and not desired:
            return {"status": "committed", "snapshot_sha256": hashlib.sha256(b"[]").hexdigest()}

        if overlap_evidence is not None:
            raise ReleaseDeploymentError("overlap migration is disabled")
        maintenance = True
        started = None
        def state_rule(item, include_id):
            result = {
                f"{key}_sha256": hashlib.sha256(json.dumps(
                    item[key], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest()
                for key in ("domain", "conditions", "action")
            }
            if include_id:
                result["id"] = item["id"]
            return result
        old_state = [state_rule(item, True) for item in old_rules]
        new_state = [state_rule(item, False) for item in desired]
        baseline = {"schema_version": 1, "phase": "prepared", "locator_pair": locator_pair,
                    "old": old_state, "new": new_state, "window_started_at": None,
                    "window_ended_at": None, "authorized_at": None, "deadline_at": None,
                    "overlap_evidence_sha256": None}
        if expected or desired:
            if prior is not None and (prior["old"] != old_state or prior["new"] != new_state
                    or prior["overlap_evidence_sha256"] != baseline["overlap_evidence_sha256"]):
                raise ReleaseDeploymentError("migration state baseline mismatch")
            if prior is None:
                persist_state(baseline)
            else:
                baseline = prior
        if maintenance:
            if filter_state not in {"old", "maintenance", "maintenance-authorized"} or locator_pair not in {"old", "new"}:
                raise ReleaseDeploymentError("unknown maintenance state; migration isolated")
            if type(max_downtime_seconds) is not int or max_downtime_seconds != 120:
                raise ReleaseDeploymentError("maximum downtime is required")
            warning = (
                "移行中はLINE WORKS通知が停止する可能性があります。コピー原本はメールボックスに残ります。"
                f"最大停止時間は{max_downtime_seconds}秒です。その間は従業員がメールボックス原本を確認してください。"
            )
            if not maintenance_resume:
                output(warning)
                answer = confirm_maintenance(warning) if confirm_maintenance else None
                if answer != "通知停止とメールボックス確認を了承します":
                    raise ReleaseDeploymentError("保守移行には明示確認が必要です")
                authorized = now()
                baseline.update(phase="maintenance-authorized",
                                authorized_at=authorized.isoformat(),
                                deadline_at=(authorized + timedelta(seconds=120)).isoformat())
                persist_state(baseline)
            if switch_pair is None:
                raise ReleaseDeploymentError("maintenance pair switch is required")

        def delete(item):
            nonlocal expected
            ensure_deadline()
            fault("before_delete")
            self.api.delete_filter(item["id"])
            fault("after_delete")
            expected = [current for current in expected if current["id"] != item["id"]]
            self._require_filter_set(expected)

        def add(item):
            nonlocal expected
            ensure_deadline()
            fault("before_add")
            result = self.api.add_filter(item)
            fault("after_add")
            new_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(new_id, str) or not new_id:
                raise ReleaseDeploymentError("new filter id was not returned")
            expected.append(dict(json.loads(json.dumps(item)), id=new_id))
            self._require_filter_set(expected)

        def ensure_deadline():
            if maintenance:
                authorized = baseline.get("authorized_at")
                deadline = baseline.get("deadline_at")
                current = now()
                if (not isinstance(authorized, str) or not isinstance(deadline, str)
                        or current < datetime.fromisoformat(authorized)
                        or current >= datetime.fromisoformat(deadline)):
                    raise ReleaseDeploymentError("maintenance deadline exceeded")

        if maintenance:
            if filter_state != "maintenance":
                existing_ids = {item["id"] for item in expected}
                for item in [rule for rule in old_rules if rule["id"] in existing_ids]:
                    delete(item)
                started = now().isoformat()
                if expected:
                    raise ReleaseDeploymentError("maintenance zero-filter readback failed")
                baseline.update(phase="maintenance-window", window_started_at=started)
                persist_state(baseline)
            else:
                started = baseline["window_started_at"]
            def ensure_time():
                ensure_deadline()
            if locator_pair == "old":
                ensure_time(); perform_switch()
            remaining = Counter(body(item) for item in desired) - Counter(body(item) for item in expected)
            for item in desired:
                key = body(item)
                if remaining[key] > 0:
                    ensure_time(); add(item); remaining[key] -= 1
            ended = now().isoformat()
            ensure_deadline()
            baseline.update(phase="committed", locator_pair="new", window_ended_at=ended)
            persist_state(baseline)
            output("二重LINE WORKS通知0、原本消失0、window中の通知停止は許容")
            return {"status": "maintenance-committed", "window_started_at": started,
                    "window_ended_at": ended}

        raise ReleaseDeploymentError("maintenance migration did not converge")
