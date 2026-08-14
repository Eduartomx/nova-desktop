from __future__ import annotations

"""Stdlib-only durable state, dependency snapshots, and rollback primitives.

The recovery journal is a fail-closed state machine.  Every write uses an
OS-backed journal lock plus compare-and-swap on ``attempt_id``/``generation``.
This module intentionally has no dependency on Nova's assistant package or on
third-party libraries so a validated copy can be used from data/recovery_runtime.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 2
RECOVERY_REQUIRED_EXIT_CODE = 7
TRANSACTION_CATEGORIES = ("modified_existing", "deleted_existing", "created_new", "unchanged")

ACTIVE_STATES = {
    "transaction_prepared",
    "files_applying",
    "files_applied",
    "dependencies_starting",
    "dependencies_running",
    "update_validation_in_progress",
    "update_validated",
    "pip_termination_unconfirmed",
    "waiting_for_processes",
    "rollback_in_progress",
    "rollback_completed",
    "rollback_validation_in_progress",
    "rollback_validation_completed",
    "dependency_repair_required",
}
ALL_STATES = ACTIVE_STATES | {"cleared"}

# Explicit state graph. Same-state writes are only allowed where an operation
# may append diagnostics/metadata without advancing the transaction.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "transaction_prepared": {"transaction_prepared", "files_applying", "rollback_in_progress"},
    "files_applying": {"files_applying", "files_applied", "rollback_in_progress"},
    "files_applied": {"files_applied", "dependencies_starting", "update_validation_in_progress", "rollback_in_progress"},
    "dependencies_starting": {"dependencies_starting", "dependencies_running", "pip_termination_unconfirmed", "rollback_in_progress"},
    "dependencies_running": {"dependencies_running", "update_validation_in_progress", "pip_termination_unconfirmed", "rollback_in_progress"},
    "update_validation_in_progress": {"update_validation_in_progress", "update_validated", "rollback_in_progress"},
    "update_validated": {"update_validated", "cleared"},
    "pip_termination_unconfirmed": {"pip_termination_unconfirmed", "waiting_for_processes", "rollback_in_progress"},
    "waiting_for_processes": {"waiting_for_processes", "rollback_in_progress"},
    "rollback_in_progress": {"rollback_in_progress", "rollback_completed"},
    "rollback_completed": {"rollback_completed", "rollback_validation_in_progress"},
    "rollback_validation_in_progress": {
        "rollback_validation_in_progress", "rollback_validation_completed", "dependency_repair_required"
    },
    "rollback_validation_completed": {"rollback_validation_completed", "cleared"},
    "dependency_repair_required": {"dependency_repair_required"},
    # A launch failure may re-quarantine a *current* cleared generation. CAS
    # prevents an older payload from performing this transition.
    "cleared": {"cleared", "rollback_validation_completed"},
}

CRITICAL_IMPORTS_BY_DISTRIBUTION = {
    "ollama": "ollama",
    "psutil": "psutil",
    "pillow": "PIL",
    "pystray": "pystray",
    "pynput": "pynput",
    "requests": "requests",
    "beautifulsoup4": "bs4",
    "openai": "openai",
    "numpy": "numpy",
    "sounddevice": "sounddevice",
    "faster-whisper": "faster_whisper",
    "pyttsx3": "pyttsx3",
    "openwakeword": "openwakeword",
    "pywinauto": "pywinauto",
    "playwright": "playwright",
}


class RecoveryJournalError(RuntimeError):
    pass


class StaleJournalWriterError(RecoveryJournalError):
    pass


@dataclass(frozen=True)
class RecoveryResult:
    pending: bool
    exit_code: int = 0
    state: str = ""
    detail: str = ""
    recovered: bool = False
    launched: bool = False
    continue_startup: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sanitize(value: object, limit: int = 500) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[: max(1, int(limit))]


def journal_path(root: Path) -> Path:
    return Path(root) / "data" / "update_recovery.json"


def journal_lock_path(root: Path) -> Path:
    return Path(root) / "data" / "update_recovery.lock"


def backup_root_path(root: Path) -> Path:
    return Path(root) / "data" / "updater_backups"


def recovery_runtime_root(root: Path) -> Path:
    return Path(root) / "data" / "recovery_runtime"


def safe_rel(value: str) -> Path:
    raw = str(value or "")
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts or not path.parts:
        raise RecoveryJournalError("unsafe_relative_path")
    return path


def _atomic_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        if os.name != "nt":
            try:
                dfd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(path, _json_bytes(payload))


_local_journal_guard = threading.RLock()


class _JournalLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        _local_journal_guard.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = open(self.path, "a+b")
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
            return self
        except Exception:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            _local_journal_guard.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.stream is not None:
                if os.name == "nt":
                    import msvcrt
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
                self.stream.close()
                self.stream = None
        finally:
            _local_journal_guard.release()


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise RecoveryJournalError("invalid_process_identity")
    pid = int(row.get("pid") or 0)
    creation = row.get("creation_time")
    if isinstance(creation, float) and not creation.is_integer():
        raise RecoveryJournalError("lossy_process_creation_time")
    creation = int(creation or 0)
    if pid <= 0 or creation <= 0:
        raise RecoveryJournalError("weak_process_identity")
    return {
        "pid": pid,
        "creation_time": creation,
        "role": _sanitize(row.get("role") or "pip_root_or_descendant", 64),
    }


def _base_payload(payload: dict[str, Any], *, schema_version: int) -> dict[str, Any]:
    attempt_id = str(payload.get("attempt_id") or "")
    generation = int(payload.get("generation") or 0)
    state = str(payload.get("state") or payload.get("status") or "")
    if len(attempt_id) < 16 or len(attempt_id) > 96:
        raise RecoveryJournalError("invalid_attempt_id")
    if generation <= 0 or state not in ALL_STATES:
        raise RecoveryJournalError("invalid_recovery_state")
    backup_path = str(payload.get("backup_path") or "")
    if state != "cleared":
        safe_rel(backup_path)
    remaining = payload.get("remaining_processes") or []
    errors = payload.get("errors") or []
    if not isinstance(remaining, list) or not isinstance(errors, list):
        raise RecoveryJournalError("invalid_recovery_lists")
    dep_path = str(payload.get("dependency_snapshot_path") or "")
    dep_sha = str(payload.get("dependency_snapshot_sha256") or "").lower()
    if dep_path:
        safe_rel(dep_path)
    if dep_sha and not re.fullmatch(r"[0-9a-f]{64}", dep_sha):
        raise RecoveryJournalError("invalid_dependency_snapshot_hash")
    result = dict(payload)
    result.update({
        "schema_version": schema_version,
        "attempt_id": attempt_id,
        "generation": generation,
        "state": state,
        "status": state,
        "backup_path": backup_path,
        "created_at": str(payload.get("created_at") or payload.get("timestamp") or _utc_now()),
        "updated_at": str(payload.get("updated_at") or payload.get("timestamp") or _utc_now()),
        "remaining_processes": [_identity(x) for x in remaining][:128],
        "errors": [_sanitize(x) for x in errors if str(x or "").strip()][:128],
        "recovery_required": state != "cleared",
        "files_may_have_changed": bool(payload.get("files_may_have_changed")),
        "dependencies_may_have_changed": bool(payload.get("dependencies_may_have_changed")),
        "dependency_snapshot_path": dep_path,
        "dependency_snapshot_sha256": dep_sha,
        "files_rollback_attempted": bool(payload.get("files_rollback_attempted")),
        "identity_verification_required": bool(payload.get("identity_verification_required")),
        "recovery_detail": _sanitize(payload.get("recovery_detail") or "", 800),
    })
    rollback_ok = payload.get("files_rollback_ok")
    result["files_rollback_ok"] = rollback_ok if rollback_ok in (True, False, None) else False
    return result


def _migrate_v1_payload(payload: dict[str, Any]) -> dict[str, Any]:
    old_state = str(payload.get("state") or payload.get("status") or "")
    state_map = {
        "pip_termination_unconfirmed": "pip_termination_unconfirmed",
        "waiting_for_processes": "waiting_for_processes",
        "rollback_in_progress": "rollback_in_progress",
        "rollback_completed": "rollback_completed",
        "validation_in_progress": "rollback_validation_in_progress",
        "validation_completed": "rollback_validation_completed",
        "cleared": "cleared",
    }
    state = state_map.get(old_state)
    if state is None:
        raise RecoveryJournalError("unsupported_schema1_state")
    migrated = dict(payload)
    migrated.update({
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "status": state,
        "files_may_have_changed": bool(payload.get("files_rollback_attempted") or old_state != "cleared"),
        "dependency_snapshot_path": "",
        "dependency_snapshot_sha256": "",
    })
    migrated = _base_payload(migrated, schema_version=SCHEMA_VERSION)
    migrated["migrated_from_schema"] = 1
    return migrated


def _migrate_unversioned(root: Path, payload: dict[str, Any], backup_root: Path | None) -> dict[str, Any]:
    if not bool(payload.get("recovery_required")):
        raise RecoveryJournalError("legacy_journal_without_recovery_requirement")
    raw_backup = payload.get("backup") or payload.get("backup_path")
    if not raw_backup:
        raise RecoveryJournalError("legacy_journal_missing_backup")
    status = str(payload.get("status") or "")
    if status == "rollback_completed_dependency_recovery":
        state, attempted, ok, identity_required = "rollback_completed", True, True, False
    elif status == "rollback_incomplete":
        state, attempted, ok, identity_required = "rollback_in_progress", True, False, False
    elif status == "pip_termination_unconfirmed":
        state, attempted, ok, identity_required = "pip_termination_unconfirmed", False, False, True
    else:
        raise RecoveryJournalError("unsupported_legacy_recovery_state")
    base = Path(backup_root) if backup_root is not None else backup_root_path(root)
    try:
        rel = Path(str(raw_backup)).resolve(strict=False).relative_to(base.resolve(strict=False)).as_posix()
    except Exception as exc:
        raise RecoveryJournalError("legacy_backup_outside_authorized_root") from exc
    now = _utc_now()
    return _base_payload({
        "schema_version": SCHEMA_VERSION,
        "attempt_id": hashlib.sha256((str(raw_backup) + "|" + status + "|" + str(payload.get("timestamp") or "")).encode("utf-8", errors="ignore")).hexdigest()[:32],
        "generation": 1,
        "state": state,
        "backup_path": rel,
        "created_at": payload.get("timestamp") or now,
        "updated_at": now,
        "dependencies_may_have_changed": bool(payload.get("dependencies_may_have_changed", True)),
        "files_may_have_changed": True,
        "files_rollback_attempted": attempted,
        "files_rollback_ok": ok,
        "remaining_processes": [],
        "errors": list(payload.get("termination_errors") or payload.get("errors") or []) + (["legacy_process_identity_unverifiable"] if identity_required else []),
        "identity_verification_required": identity_required,
        "recovery_detail": _sanitize(payload.get("recovery_detail") or payload.get("message") or "legacy recovery migrated", 800),
    }, schema_version=SCHEMA_VERSION)


def validate_journal(payload: dict[str, Any], *, root: Path | None = None, backup_root: Path | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RecoveryJournalError("journal_not_object")
    version = int(payload.get("schema_version") or 0)
    if version == SCHEMA_VERSION:
        return _base_payload(payload, schema_version=SCHEMA_VERSION)
    if version == 1:
        return _migrate_v1_payload(payload)
    if version == 0 and root is not None:
        return _migrate_unversioned(Path(root), payload, backup_root)
    raise RecoveryJournalError("unsupported_recovery_schema")


def _read_raw_journal(root: Path) -> dict[str, Any] | None:
    path = journal_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecoveryJournalError(f"recovery_journal_corrupt:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RecoveryJournalError("recovery_journal_corrupt:not_object")
    return payload


def load_journal(root: Path, *, backup_root: Path | None = None, migrate: bool = True) -> dict[str, Any] | None:
    payload = _read_raw_journal(root)
    if payload is None:
        return None
    version = int(payload.get("schema_version") or 0)
    if version == SCHEMA_VERSION:
        return validate_journal(payload, root=root, backup_root=backup_root)
    if not migrate:
        raise RecoveryJournalError("recovery_schema_migration_required")
    # Migration is intentionally returned in-memory. It is persisted only by a
    # later CAS transition while recovery owns its supervisor/runtime locks.
    return validate_journal(payload, root=root, backup_root=backup_root)


def _compare_current(root: Path, expected: dict[str, Any], *, backup_root: Path | None = None) -> dict[str, Any]:
    raw = _read_raw_journal(root)
    if raw is None:
        raise StaleJournalWriterError("journal_disappeared")
    current = validate_journal(raw, root=root, backup_root=backup_root)
    expected = validate_journal(expected, root=root, backup_root=backup_root)
    if current["attempt_id"] != expected["attempt_id"]:
        raise StaleJournalWriterError("journal_attempt_changed")
    if int(current["generation"]) != int(expected["generation"]):
        raise StaleJournalWriterError("journal_generation_changed")
    return current


def transition_journal(root: Path, payload: dict[str, Any], state: str, *, backup_root: Path | None = None, **updates: Any) -> dict[str, Any]:
    root = Path(root)
    if state not in ALL_STATES:
        raise RecoveryJournalError("invalid_transition_state")
    with _JournalLock(journal_lock_path(root)):
        current = _compare_current(root, payload, backup_root=backup_root)
        if state not in ALLOWED_TRANSITIONS.get(current["state"], set()):
            raise RecoveryJournalError(f"transition_not_allowed:{current['state']}->{state}")
        nxt = dict(current)
        nxt.update(updates)
        nxt.update({
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "status": state,
            "generation": int(current["generation"]) + 1,
            "updated_at": _utc_now(),
            "recovery_required": state != "cleared",
        })
        if state == "cleared":
            nxt["remaining_processes"] = []
            nxt["identity_verification_required"] = False
        nxt = validate_journal(nxt, root=root, backup_root=backup_root)
        _atomic_json(journal_path(root), nxt)
        return nxt


def append_journal_error(root: Path, payload: dict[str, Any], error: str, *, backup_root: Path | None = None) -> dict[str, Any]:
    current = validate_journal(payload, root=root, backup_root=backup_root)
    errors = list(current.get("errors") or []) + [_sanitize(error, 500)]
    return transition_journal(root, current, current["state"], backup_root=backup_root, errors=errors[-128:])
