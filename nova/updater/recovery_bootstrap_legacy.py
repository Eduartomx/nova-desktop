from __future__ import annotations

"""Minimal startup/update recovery coordinator.

This module and recovery_state use only the Python standard library so app.py can
run the quarantine gate before importing Nova's assistant stack or third-party
packages.
"""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from .recovery_state import (
        RECOVERY_REQUIRED_EXIT_CODE, RecoveryJournalError, RecoveryResult,
        _sanitize, load_journal, transition_journal, evaluate_remaining_processes,
        resolve_backup, restore_backup_idempotent, validate_restored_install,
        create_quarantine_journal, create_rollback_recovery_journal,
    )
except ImportError:
    from recovery_state import (
        RECOVERY_REQUIRED_EXIT_CODE, RecoveryJournalError, RecoveryResult,
        _sanitize, load_journal, transition_journal, evaluate_remaining_processes,
        resolve_backup, restore_backup_idempotent, validate_restored_install,
        create_quarantine_journal, create_rollback_recovery_journal,
    )


def _hash_text(value: str, length: int = 24) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:length]


class _ScopedFileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.stream = None
        self.acquired = False

    def acquire(self) -> bool:
        if self.acquired:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            stream.close()
            return False
        self.stream = stream
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired or self.stream is None:
            return
        stream = self.stream
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self.stream = None
            self.acquired = False


def _windows_scope_identity() -> tuple[str, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_uint, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    token = wintypes.HANDLE()
    TOKEN_QUERY = 0x0008
    TokenUser = 1
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents.value
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = str(sid_text.value or "")
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        if not sid:
            raise RuntimeError("empty_user_sid")
        return _hash_text(sid), int(session.value)
    finally:
        kernel32.CloseHandle(token)


def _scope_directory() -> Path:
    if os.name == "nt":
        try:
            user_hash, session_id = _windows_scope_identity()
        except Exception:
            raw = "|".join((os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""), str(Path.home())))
            user_hash = _hash_text(raw)
            session_id = int(_hash_text(os.environ.get("SESSIONNAME") or "default", 8), 16)
    else:
        raw = "|".join((os.environ.get("USER", ""), os.environ.get("USERNAME", ""), os.environ.get("HOME", "")))
        user_hash = _hash_text(raw or str(Path.home()))
        session = os.environ.get("XDG_SESSION_ID") or os.environ.get("SESSIONNAME") or "default"
        session_id = int(_hash_text(session, 8), 16)
    scope_id = _hash_text(f"{user_hash}|{session_id}")
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".nova")) / "Nova" / "runtime"
    return base / f"scope-{scope_id}"


def _supervisor_lock() -> _ScopedFileLock:
    return _ScopedFileLock(_scope_directory() / "update_supervisor.lock")


def _runtime_guard() -> _ScopedFileLock:
    return _ScopedFileLock(_scope_directory() / "runtime.lock")


def _append_error(root: Path, payload: dict[str, Any], error: str) -> dict[str, Any]:
    errors = list(payload.get("errors") or [])
    errors.append(_sanitize(error, 500))
    return transition_journal(root, payload, str(payload.get("state") or "waiting_for_processes"), errors=errors[-128:])


def _launch_post_recovery(root: Path, launcher=None) -> tuple[bool, str]:
    command = [sys.executable, str(Path(root) / "app.py"), "--post-recovery"]
    try:
        launch = launcher or subprocess.Popen
        launch(command, cwd=str(root), close_fds=True)
        return True, ""
    except Exception as exc:
        return False, f"post_recovery_launch_failed:{type(exc).__name__}"


def recover_pending(
    root: Path,
    *,
    supervisor_already_held: bool = False,
    inspector=None,
    backup_root: Path | None = None,
    restore_func=None,
    validator=None,
    launcher=None,
    launch_after_success: bool = True,
    lock_factories=None,
) -> RecoveryResult:
    root = Path(root)
    try:
        journal = load_journal(root, backup_root=backup_root)
    except RecoveryJournalError as exc:
        return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "corrupt", _sanitize(exc), continue_startup=False)
    if journal is None or journal.get("state") == "cleared" or not journal.get("recovery_required"):
        return RecoveryResult(False, 0, "cleared", "", continue_startup=True)

    blocking, inspect_errors = evaluate_remaining_processes(journal, inspector=inspector)
    if journal.get("identity_verification_required"):
        return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, str(journal["state"]), "legacy process identity cannot be verified automatically")
    if blocking or inspect_errors:
        return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "waiting_for_processes", "waiting for strong process identities")

    supervisor = None
    runtime = None
    factories = lock_factories or {}
    try:
        if not supervisor_already_held:
            supervisor = (factories.get("supervisor") or _supervisor_lock)()
            if not supervisor.acquire():
                return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, str(journal["state"]), "recovery supervisor already active")
        runtime = (factories.get("runtime") or _runtime_guard)()
        if not runtime.acquire():
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, str(journal["state"]), "runtime/recovery guard is busy")

        journal = load_journal(root, backup_root=backup_root)
        if journal is None or journal.get("state") == "cleared" or not journal.get("recovery_required"):
            return RecoveryResult(False, 0, "cleared", "", continue_startup=True)
        blocking, inspect_errors = evaluate_remaining_processes(journal, inspector=inspector)
        if journal.get("identity_verification_required") or blocking or inspect_errors:
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "waiting_for_processes", "process identity no longer safe to auto-recover")

        backup = resolve_backup(root, journal, backup_root=backup_root)
        state = str(journal["state"])
        restore = restore_func or restore_backup_idempotent
        validate = validator or validate_restored_install

        if state in {"pip_termination_unconfirmed", "waiting_for_processes", "rollback_in_progress"}:
            journal = transition_journal(root, journal, "rollback_in_progress", remaining_processes=[])
            try:
                restore(root, backup)
            except Exception as exc:
                journal = _append_error(root, journal, f"rollback_failed:{type(exc).__name__}:{exc}")
                return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "rollback_in_progress", "rollback failed")
            journal = transition_journal(root, journal, "rollback_completed", files_rollback_attempted=True, files_rollback_ok=True)
            state = "rollback_completed"

        if state in {"rollback_completed", "validation_in_progress"}:
            journal = transition_journal(root, journal, "validation_in_progress")
            try:
                valid, detail = validate(root)
            except Exception as exc:
                valid, detail = False, f"validation_exception:{type(exc).__name__}"
            if not valid:
                journal = _append_error(root, journal, detail)
                return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "validation_in_progress", _sanitize(detail))
            journal = transition_journal(root, journal, "validation_completed", validation_detail=_sanitize(detail, 500))
            state = "validation_completed"

        if state == "validation_completed":
            journal = transition_journal(
                root, journal, "cleared", recovery_required=False,
                files_rollback_attempted=True, files_rollback_ok=True,
            )

        # Runtime/recovery guard must be released before the recovered Nova can
        # acquire runtime.lock. The supervisor mutex remains held across launch.
        runtime.release()
        runtime = None
        launched = False
        if launch_after_success:
            launched, launch_error = _launch_post_recovery(root, launcher=launcher)
            if not launched:
                try:
                    cleared = load_journal(root, backup_root=backup_root)
                    if cleared is not None and cleared.get("state") == "cleared":
                        transition_journal(
                            root, cleared, "cleared",
                            errors=(list(cleared.get("errors") or []) + [launch_error])[-128:],
                        )
                except Exception:
                    pass
                return RecoveryResult(False, RECOVERY_REQUIRED_EXIT_CODE, "cleared", launch_error, recovered=True, launched=False)
        return RecoveryResult(
            False, 0, "cleared", "recovery completed", recovered=True,
            launched=launched, continue_startup=not launch_after_success,
        )
    except RecoveryJournalError as exc:
        return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "invalid", _sanitize(exc))
    except Exception as exc:
        try:
            current = load_journal(root, backup_root=backup_root)
            if current is not None and current.get("state") != "cleared":
                _append_error(root, current, f"recovery_exception:{type(exc).__name__}:{exc}")
        except Exception:
            pass
        return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "failed", f"recovery_failed:{type(exc).__name__}")
    finally:
        if runtime is not None:
            try:
                runtime.release()
            except Exception:
                pass
        if supervisor is not None:
            try:
                supervisor.release()
            except Exception:
                pass


def _minimal_notice(root: Path, result: RecoveryResult) -> None:
    state = _sanitize(result.state or "recovery_required", 80)
    detail = _sanitize(result.detail or "Nova detectó una recuperación pendiente.", 240)
    safe_location = str(Path(root) / "data")
    message = (
        "Nova detectó una recuperación pendiente y no iniciará el asistente normal.\n\n"
        f"Estado: {state}\n"
        f"Detalle: {detail}\n\n"
        f"Diagnóstico/backup: {safe_location}\n"
        "No borres update_recovery.json ni el backup manualmente mientras la recuperación esté pendiente."
    )
    if os.name == "nt":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
            user32.MessageBoxW.restype = ctypes.c_int
            user32.MessageBoxW(None, message, "Nova · Recuperación requerida", 0x00000030)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def startup_recovery_gate(
    root: Path, *, inspector=None, backup_root: Path | None = None,
    launcher=None, lock_factories=None, show_notice: bool = True,
) -> RecoveryResult:
    root = Path(root)
    result = recover_pending(
        root, supervisor_already_held=False, inspector=inspector,
        backup_root=backup_root, launcher=launcher, launch_after_success=True,
        lock_factories=lock_factories,
    )
    if result.continue_startup:
        return result
    if result.recovered and result.launched:
        return result
    if show_notice:
        _minimal_notice(root, result)
    return result


def updater_recovery_gate(
    root: Path, *, supervisor_already_held: bool = True, inspector=None,
    backup_root: Path | None = None, launcher=None, lock_factories=None,
) -> RecoveryResult:
    """Block a new update and recover instead; the requested update never resumes."""
    result = recover_pending(
        root, supervisor_already_held=supervisor_already_held, inspector=inspector,
        backup_root=backup_root, launcher=launcher, launch_after_success=True,
        lock_factories=lock_factories,
    )
    if not result.pending and not result.recovered and result.continue_startup:
        return RecoveryResult(False, 0, "", "no recovery pending", continue_startup=True)
    return RecoveryResult(
        True, RECOVERY_REQUIRED_EXIT_CODE, result.state,
        result.detail or "recovery handled before update",
        recovered=result.recovered, launched=result.launched,
    )


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Nova persistent recovery bootstrap")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        journal = load_journal(root)
    except RecoveryJournalError as exc:
        print(f"RECOVERY_BLOCKED {exc}")
        return RECOVERY_REQUIRED_EXIT_CODE
    if args.status:
        if journal is None:
            print("NO_RECOVERY")
            return 0
        print(json.dumps({
            key: journal.get(key) for key in (
                "schema_version", "attempt_id", "generation", "state",
                "recovery_required", "backup_path", "updated_at",
            )
        }, ensure_ascii=False, indent=2))
        return RECOVERY_REQUIRED_EXIT_CODE if journal.get("recovery_required") else 0
    if args.recover:
        result = recover_pending(root, launch_after_success=False)
        print(result.detail or result.state)
        return int(result.exit_code)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
