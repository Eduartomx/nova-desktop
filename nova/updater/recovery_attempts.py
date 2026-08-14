from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import uuid
from typing import Any, Callable

try:
    from .recovery_journal import (
        ALLOWED_TRANSITIONS, SCHEMA_VERSION, RecoveryJournalError,
        _JournalLock, _identity, backup_root_path, _atomic_json, _read_raw_journal, _sanitize, _utc_now,
        journal_lock_path, journal_path, load_journal, safe_rel,
        transition_journal, validate_journal,
    )
except ImportError:
    from recovery_journal import (
        ALLOWED_TRANSITIONS, SCHEMA_VERSION, RecoveryJournalError,
        _JournalLock, _identity, backup_root_path, _atomic_json, _read_raw_journal, _sanitize, _utc_now,
        journal_lock_path, journal_path, load_journal, safe_rel,
        transition_journal, validate_journal,
    )

def _backup_rel(root: Path, backup: Path, backup_root: Path | None) -> str:
    base = Path(backup_root) if backup_root is not None else backup_root_path(root)
    try:
        rel = Path(backup).resolve(strict=False).relative_to(base.resolve(strict=False)).as_posix()
    except ValueError as exc:
        raise RecoveryJournalError("backup_outside_authorized_root") from exc
    safe_rel(rel)
    return rel


def create_transaction_journal(
    root: Path,
    backup: Path,
    *,
    backup_root: Path | None = None,
    attempt_id: str | None = None,
    dependency_snapshot_path: str = "",
    dependency_snapshot_sha256: str = "",
) -> dict[str, Any]:
    root = Path(root)
    now = _utc_now()
    payload = validate_journal({
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id or uuid.uuid4().hex,
        "generation": 1,
        "state": "transaction_prepared",
        "backup_path": _backup_rel(root, backup, backup_root),
        "created_at": now,
        "updated_at": now,
        "recovery_required": True,
        "files_may_have_changed": False,
        "dependencies_may_have_changed": False,
        "dependency_snapshot_path": dependency_snapshot_path,
        "dependency_snapshot_sha256": dependency_snapshot_sha256,
        "files_rollback_attempted": False,
        "files_rollback_ok": None,
        "remaining_processes": [],
        "errors": [],
        "identity_verification_required": False,
    }, root=root, backup_root=backup_root)
    with _JournalLock(journal_lock_path(root)):
        existing_raw = _read_raw_journal(root)
        if existing_raw is not None:
            existing = validate_journal(existing_raw, root=root, backup_root=backup_root)
            if existing.get("state") != "cleared" or existing.get("recovery_required"):
                raise RecoveryJournalError("active_recovery_journal_exists")
        _atomic_json(journal_path(root), payload)
    return payload


def create_quarantine_journal(
    root: Path,
    backup: Path,
    *,
    remaining_processes: list[dict[str, Any]],
    errors: list[str] | None = None,
    recovery_detail: str = "",
    backup_root: Path | None = None,
    attempt_id: str | None = None,
    identity_verification_required: bool = False,
) -> dict[str, Any]:
    existing = load_journal(root, backup_root=backup_root)
    rel = _backup_rel(root, backup, backup_root)
    if existing is not None and existing.get("state") != "cleared" and existing.get("backup_path") == rel:
        state = existing["state"]
        if "pip_termination_unconfirmed" in ALLOWED_TRANSITIONS.get(state, set()):
            return transition_journal(
                root, existing, "pip_termination_unconfirmed", backup_root=backup_root,
                remaining_processes=remaining_processes,
                errors=errors or existing.get("errors") or [],
                dependencies_may_have_changed=True,
                recovery_detail=_sanitize(recovery_detail, 800),
                identity_verification_required=bool(identity_verification_required),
            )
    now = _utc_now()
    payload = validate_journal({
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id or uuid.uuid4().hex,
        "generation": 1,
        "state": "pip_termination_unconfirmed",
        "backup_path": rel,
        "created_at": now,
        "updated_at": now,
        "files_may_have_changed": True,
        "dependencies_may_have_changed": True,
        "dependency_snapshot_path": "",
        "dependency_snapshot_sha256": "",
        "files_rollback_attempted": False,
        "files_rollback_ok": False,
        "remaining_processes": remaining_processes,
        "errors": errors or [],
        "identity_verification_required": bool(identity_verification_required),
        "recovery_detail": _sanitize(recovery_detail, 800),
    }, root=root, backup_root=backup_root)
    with _JournalLock(journal_lock_path(root)):
        current = _read_raw_journal(root)
        if current is not None:
            checked = validate_journal(current, root=root, backup_root=backup_root)
            if checked.get("state") != "cleared":
                raise RecoveryJournalError("active_recovery_journal_exists")
        _atomic_json(journal_path(root), payload)
    return payload


def create_rollback_recovery_journal(
    root: Path,
    backup: Path,
    *,
    rollback_ok: bool,
    dependencies_may_have_changed: bool,
    errors: list[str] | None = None,
    recovery_detail: str = "",
    backup_root: Path | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    existing = load_journal(root, backup_root=backup_root)
    rel = _backup_rel(root, backup, backup_root)
    target = "rollback_completed" if rollback_ok else "rollback_in_progress"
    if existing is not None and existing.get("state") != "cleared" and existing.get("backup_path") == rel:
        current = existing
        if current["state"] != "rollback_in_progress":
            current = transition_journal(root, current, "rollback_in_progress", backup_root=backup_root,
                                         dependencies_may_have_changed=bool(dependencies_may_have_changed))
        if rollback_ok:
            current = transition_journal(root, current, "rollback_completed", backup_root=backup_root,
                                         files_rollback_attempted=True, files_rollback_ok=True)
        return current
    now = _utc_now()
    payload = validate_journal({
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id or uuid.uuid4().hex,
        "generation": 1,
        "state": target,
        "backup_path": rel,
        "created_at": now,
        "updated_at": now,
        "files_may_have_changed": True,
        "dependencies_may_have_changed": bool(dependencies_may_have_changed),
        "dependency_snapshot_path": "",
        "dependency_snapshot_sha256": "",
        "files_rollback_attempted": True,
        "files_rollback_ok": bool(rollback_ok),
        "remaining_processes": [],
        "errors": errors or [],
        "identity_verification_required": False,
        "recovery_detail": _sanitize(recovery_detail, 800),
    }, root=root, backup_root=backup_root)
    with _JournalLock(journal_lock_path(root)):
        current = _read_raw_journal(root)
        if current is not None and validate_journal(current, root=root, backup_root=backup_root).get("state") != "cleared":
            raise RecoveryJournalError("active_recovery_journal_exists")
        _atomic_json(journal_path(root), payload)
    return payload


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _windows_creation(pid: int) -> tuple[str, int | None, str]:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]
    k.GetProcessTimes.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    handle = k.OpenProcess(0x1000, False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        return ("gone", None, "") if error in (87, 1168) else ("unknown", None, f"process_open_failed:{error}")
    try:
        created = _FILETIME(); exited = _FILETIME(); kernel = _FILETIME(); user = _FILETIME()
        if not k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return "unknown", None, f"process_times_failed:{ctypes.get_last_error()}"
        value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return "alive", value if value > 0 else None, ""
    finally:
        k.CloseHandle(handle)


def _linux_creation(pid: int) -> tuple[str, int | None, str]:
    try:
        text = (Path("/proc") / str(int(pid)) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return "gone", None, ""
    except Exception as exc:
        return "unknown", None, f"proc_stat_failed:{type(exc).__name__}"
    try:
        tail = text[text.rfind(")") + 2:].split()
        ticks = int(tail[19])
        btime = next(int(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime "))
        hz = int(os.sysconf("SC_CLK_TCK"))
        return "alive", btime * 1_000_000 + ticks * 1_000_000 // hz, ""
    except Exception as exc:
        return "unknown", None, f"proc_identity_failed:{type(exc).__name__}"


def inspect_process_identity(identity: dict[str, Any]) -> tuple[str, str]:
    row = _identity(identity)
    if os.name == "nt":
        state, creation, error = _windows_creation(row["pid"])
    elif sys.platform.startswith("linux"):
        state, creation, error = _linux_creation(row["pid"])
    else:
        return "unknown", "strong_identity_unsupported_platform"
    if state in ("gone", "unknown"):
        return state, error
    if creation is None:
        return "unknown", "process_identity_unavailable"
    return ("alive", "") if int(creation) == row["creation_time"] else ("reused", "")


def evaluate_remaining_processes(payload: dict[str, Any], *, inspector: Callable[[dict[str, Any]], tuple[str, str]] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    journal = validate_journal(payload)
    if journal.get("identity_verification_required"):
        return [], ["legacy_process_identity_unverifiable"]
    check = inspector or inspect_process_identity
    blocking: list[dict[str, Any]] = []
    errors: list[str] = []
    for identity in journal["remaining_processes"]:
        try:
            state, error = check(identity)
        except Exception as exc:
            state, error = "unknown", f"identity_check_failed:{type(exc).__name__}"
        if state == "alive":
            blocking.append(identity)
        elif state == "unknown":
            blocking.append(identity)
            errors.append(_sanitize(error or "process_identity_unknown", 300))
        elif state not in ("gone", "reused"):
            blocking.append(identity)
            errors.append("process_identity_invalid_state")
    return blocking, errors
