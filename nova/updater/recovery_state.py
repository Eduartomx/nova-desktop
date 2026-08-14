from __future__ import annotations

"""Stdlib-only durable state and rollback primitives for resident recovery."""

from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from typing import Any, Callable

SCHEMA_VERSION = 1
RECOVERY_REQUIRED_EXIT_CODE = 7
ACTIVE_STATES = {
    "pip_termination_unconfirmed", "waiting_for_processes", "rollback_in_progress",
    "rollback_completed", "validation_in_progress", "validation_completed",
}
ALL_STATES = ACTIVE_STATES | {"cleared"}
TRANSACTION_CATEGORIES = ("modified_existing", "deleted_existing", "created_new", "unchanged")


class RecoveryJournalError(RuntimeError):
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
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:max(1, int(limit))]


def journal_path(root: Path) -> Path:
    return Path(root) / "data" / "update_recovery.json"


def backup_root_path(root: Path) -> Path:
    return Path(root) / "data" / "updater_backups"


def safe_rel(value: str) -> Path:
    raw = str(value or "")
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts or not path.parts:
        raise RecoveryJournalError("unsafe_relative_path")
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
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


def validate_journal(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RecoveryJournalError("journal_not_object")
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise RecoveryJournalError("unsupported_recovery_schema")
    attempt_id = str(payload.get("attempt_id") or "")
    generation = int(payload.get("generation") or 0)
    state = str(payload.get("state") or "")
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
    result = dict(payload)
    result.update({
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "generation": generation,
        "state": state,
        "status": state,
        "backup_path": backup_path,
        "remaining_processes": [_identity(x) for x in remaining][:128],
        "errors": [_sanitize(x) for x in errors if str(x or "").strip()][:128],
        "recovery_required": state != "cleared",
        "dependencies_may_have_changed": bool(payload.get("dependencies_may_have_changed")),
        "files_rollback_attempted": bool(payload.get("files_rollback_attempted")),
        "identity_verification_required": bool(payload.get("identity_verification_required")),
    })
    rollback_ok = payload.get("files_rollback_ok")
    result["files_rollback_ok"] = rollback_ok if rollback_ok in (True, False, None) else False
    return result


def _backup_rel(root: Path, backup: Path, backup_root: Path | None) -> str:
    base = Path(backup_root) if backup_root is not None else backup_root_path(root)
    try:
        rel = Path(backup).resolve(strict=False).relative_to(base.resolve(strict=False)).as_posix()
    except ValueError as exc:
        raise RecoveryJournalError("backup_outside_authorized_root") from exc
    safe_rel(rel)
    return rel


def _legacy(root: Path, payload: dict[str, Any], backup_root: Path | None) -> dict[str, Any]:
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
    errors = [_sanitize(x) for x in (payload.get("termination_errors") or payload.get("errors") or [])]
    if identity_required:
        errors.append("legacy_process_identity_unverifiable")
    now = _utc_now()
    return validate_journal({
        "schema_version": 1, "attempt_id": uuid.uuid4().hex, "generation": 1,
        "state": state, "backup_path": rel, "created_at": payload.get("timestamp") or now,
        "updated_at": now, "recovery_required": True,
        "dependencies_may_have_changed": bool(payload.get("dependencies_may_have_changed", True)),
        "files_rollback_attempted": attempted, "files_rollback_ok": ok,
        "remaining_processes": [], "errors": errors,
        "identity_verification_required": identity_required,
        "recovery_detail": _sanitize(payload.get("recovery_detail") or payload.get("message") or "legacy recovery migrated", 800),
    })


def load_journal(root: Path, *, backup_root: Path | None = None, migrate: bool = True) -> dict[str, Any] | None:
    path = journal_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecoveryJournalError(f"recovery_journal_corrupt:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RecoveryJournalError("recovery_journal_corrupt:not_object")
    if "schema_version" not in payload:
        if not migrate:
            raise RecoveryJournalError("legacy_recovery_journal")
        payload = _legacy(root, payload, backup_root)
        _atomic_json(path, payload)
    return validate_journal(payload)


def transition_journal(root: Path, payload: dict[str, Any], state: str, **updates: Any) -> dict[str, Any]:
    if state not in ALL_STATES:
        raise RecoveryJournalError("invalid_transition_state")
    current = validate_journal(payload)
    nxt = dict(current)
    nxt.update(updates)
    nxt.update({"state": state, "status": state, "generation": current["generation"] + 1, "updated_at": _utc_now(), "recovery_required": state != "cleared"})
    if state == "cleared":
        nxt["remaining_processes"] = []
        nxt["identity_verification_required"] = False
    nxt = validate_journal(nxt)
    _atomic_json(journal_path(root), nxt)
    return nxt


def create_quarantine_journal(root: Path, backup: Path, *, remaining_processes: list[dict[str, Any]], errors: list[str] | None = None, recovery_detail: str = "", backup_root: Path | None = None, attempt_id: str | None = None, identity_verification_required: bool = False) -> dict[str, Any]:
    now = _utc_now()
    payload = validate_journal({
        "schema_version": 1, "attempt_id": attempt_id or uuid.uuid4().hex, "generation": 1,
        "state": "pip_termination_unconfirmed", "backup_path": _backup_rel(root, backup, backup_root),
        "created_at": now, "updated_at": now, "recovery_required": True,
        "dependencies_may_have_changed": True, "files_rollback_attempted": False,
        "files_rollback_ok": False, "remaining_processes": remaining_processes,
        "errors": errors or [], "identity_verification_required": bool(identity_verification_required),
        "recovery_detail": _sanitize(recovery_detail, 800),
    })
    _atomic_json(journal_path(root), payload)
    return payload


def create_rollback_recovery_journal(root: Path, backup: Path, *, rollback_ok: bool, dependencies_may_have_changed: bool, errors: list[str] | None = None, recovery_detail: str = "", backup_root: Path | None = None, attempt_id: str | None = None) -> dict[str, Any]:
    now = _utc_now()
    state = "rollback_completed" if rollback_ok else "rollback_in_progress"
    payload = validate_journal({
        "schema_version": 1, "attempt_id": attempt_id or uuid.uuid4().hex, "generation": 1,
        "state": state, "backup_path": _backup_rel(root, backup, backup_root),
        "created_at": now, "updated_at": now, "recovery_required": True,
        "dependencies_may_have_changed": bool(dependencies_may_have_changed),
        "files_rollback_attempted": True, "files_rollback_ok": bool(rollback_ok),
        "remaining_processes": [], "errors": errors or [], "identity_verification_required": False,
        "recovery_detail": _sanitize(recovery_detail, 800),
    })
    _atomic_json(journal_path(root), payload)
    return payload


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _windows_creation(pid: int) -> tuple[str, int | None, str]:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]; k.OpenProcess.restype = wintypes.HANDLE
    k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]; k.GetProcessTimes.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]; k.CloseHandle.restype = wintypes.BOOL
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
    blocking, errors = [], []
    for identity in journal["remaining_processes"]:
        try:
            state, error = check(identity)
        except Exception as exc:
            state, error = "unknown", f"identity_check_failed:{type(exc).__name__}"
        if state == "alive":
            blocking.append(identity)
        elif state == "unknown":
            blocking.append(identity); errors.append(_sanitize(error or "process_identity_unknown", 300))
        elif state not in ("gone", "reused"):
            blocking.append(identity); errors.append("process_identity_invalid_state")
    return blocking, errors


def _reject_symlink_chain(base: Path, target: Path) -> None:
    base = Path(base).resolve(strict=True)
    try:
        rel = Path(target).relative_to(base)
    except ValueError as exc:
        raise RecoveryJournalError("path_outside_authorized_root") from exc
    current = base
    for part in rel.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise RecoveryJournalError("unsafe_symlink_in_recovery_path")


def resolve_backup(root: Path, payload: dict[str, Any], *, backup_root: Path | None = None) -> Path:
    journal = validate_journal(payload)
    base = Path(backup_root) if backup_root is not None else backup_root_path(root)
    if not base.is_dir() or base.is_symlink():
        raise RecoveryJournalError("authorized_backup_root_unavailable")
    base = base.resolve(strict=True)
    candidate = base / safe_rel(journal["backup_path"])
    _reject_symlink_chain(base, candidate)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise RecoveryJournalError("backup_resolves_outside_authorized_root") from exc
    manifest = resolved / "backup.json"
    if not resolved.is_dir() or resolved.is_symlink() or not manifest.is_file() or manifest.is_symlink():
        raise RecoveryJournalError("backup_or_manifest_invalid")
    return resolved


def _install_target(root: Path, rel: str) -> Path:
    base = Path(root).resolve(strict=True)
    path = safe_rel(rel)
    target = base / path
    current = base
    for part in path.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise RecoveryJournalError("install_symlink_rejected")
    try:
        target.resolve(strict=False).relative_to(base)
    except ValueError as exc:
        raise RecoveryJournalError("install_target_outside_root") from exc
    return target


def _atomic_copy(src: Path, dst: Path) -> None:
    if not src.is_file() or src.is_symlink():
        raise RecoveryJournalError("unsafe_or_missing_backup_file")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".", suffix=".recovery-tmp", dir=str(dst.parent)); os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        with open(tmp, "rb") as stream:
            try: os.fsync(stream.fileno())
            except OSError: pass
        os.replace(tmp, dst)
    finally:
        try: tmp.unlink(missing_ok=True)
        except OSError: pass


def _manifest_lists(meta: dict[str, Any]) -> dict[str, list[str]]:
    if not isinstance(meta, dict) or int(meta.get("schema") or 0) != 2:
        raise RecoveryJournalError("backup_manifest_schema_invalid")
    result = {}
    for key in TRANSACTION_CATEGORIES:
        rows = meta.get(key) or []
        if not isinstance(rows, list):
            raise RecoveryJournalError("backup_manifest_category_invalid")
        result[key] = sorted({safe_rel(str(x)).as_posix() for x in rows})
    return result


def restore_backup_idempotent(root: Path, backup: Path) -> None:
    backup = Path(backup)
    manifest_path = backup / "backup.json"
    if manifest_path.is_symlink():
        raise RecoveryJournalError("backup_manifest_symlink_rejected")
    try:
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecoveryJournalError(f"backup_manifest_corrupt:{type(exc).__name__}") from exc
    lists = _manifest_lists(meta)
    for rel in lists["modified_existing"] + lists["deleted_existing"]:
        src = backup / "files" / safe_rel(rel); _reject_symlink_chain(backup, src)
        _atomic_copy(src, _install_target(root, rel))
    for rel in lists["created_new"]:
        target = _install_target(root, rel)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise RecoveryJournalError("created_target_invalid")
        target.unlink(missing_ok=True)
    managed = meta.get("managed_files")
    if not isinstance(managed, dict):
        raise RecoveryJournalError("managed_files_state_missing")
    target = _install_target(root, str(managed.get("path") or ""))
    if target != _install_target(root, "updater/managed_files.json"):
        raise RecoveryJournalError("managed_files_path_mismatch")
    if bool(managed.get("existed")):
        src = backup / safe_rel(str(managed.get("backup") or "")); _reject_symlink_chain(backup, src)
        _atomic_copy(src, target)
    else:
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise RecoveryJournalError("managed_files_invalid")
        target.unlink(missing_ok=True)


def _requirement_names(root: Path) -> list[str]:
    req = Path(root) / "requirements.txt"
    if not req.is_file() or req.is_symlink():
        return []
    names = []
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")) or "://" in line:
            continue
        token = line.split(";", 1)[0].strip()
        for sep in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
            if sep in token: token = token.split(sep, 1)[0].strip(); break
        if "[" in token: token = token.split("[", 1)[0].strip()
        if token: names.append(token)
    return sorted(set(names))


def validate_restored_install(root: Path) -> tuple[bool, str]:
    root = Path(root)
    for path in (root / "app.py", root / "updater" / "nova_updater.py", root / "updater" / "update_runner.py"):
        if not path.is_file() or path.is_symlink():
            return False, f"required_file_invalid:{path.name}"
    targets = [root / "app.py"]
    for directory in (root / "assistant", root / "updater"):
        if directory.is_dir() and not directory.is_symlink():
            targets.extend(sorted(directory.glob("*.py")))
    for path in targets:
        try:
            if path.is_symlink(): return False, f"python_symlink_rejected:{path.name}"
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:
            return False, f"python_validation_failed:{path.name}:{type(exc).__name__}"
    managed = root / "updater" / "managed_files.json"
    try:
        if managed.is_symlink(): return False, "managed_files_symlink_rejected"
        if managed.exists():
            for rel in json.loads(managed.read_text(encoding="utf-8")).get("files", []): safe_rel(str(rel))
    except Exception as exc:
        return False, f"managed_files_validation_failed:{type(exc).__name__}"
    missing = []
    for name in _requirement_names(root):
        try: importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: missing.append(name)
        except Exception as exc: return False, f"dependency_metadata_failed:{type(exc).__name__}"
    if missing:
        return False, "missing_declared_distributions:" + ",".join(missing[:12])
    return True, "restored_files_and_declared_distributions_validated"
