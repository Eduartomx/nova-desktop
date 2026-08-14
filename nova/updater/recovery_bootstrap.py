from __future__ import annotations

"""Stdlib-only recovery coordinator for Nova's persistent update journal."""

import argparse
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
        RECOVERY_REQUIRED_EXIT_CODE,
        RecoveryJournalError,
        RecoveryResult,
        _sanitize,
        append_journal_error,
        create_quarantine_journal,
        create_rollback_recovery_journal,
        evaluate_remaining_processes,
        load_journal,
        resolve_backup,
        restore_backup_idempotent,
        transition_journal,
        validate_restored_install,
    )
except ImportError:
    from recovery_state import (
        RECOVERY_REQUIRED_EXIT_CODE,
        RecoveryJournalError,
        RecoveryResult,
        _sanitize,
        append_journal_error,
        create_quarantine_journal,
        create_rollback_recovery_journal,
        evaluate_remaining_processes,
        load_journal,
        resolve_backup,
        restore_backup_idempotent,
        transition_journal,
        validate_restored_install,
    )

ROLLBACK_ENTRY_STATES = {
    "transaction_prepared",
    "files_applying",
    "files_applied",
    "dependencies_starting",
    "dependencies_running",
    "update_validation_in_progress",
    "pip_termination_unconfirmed",
    "waiting_for_processes",
    "rollback_in_progress",
}


try:
    from .recovery_locking import _runtime_guard, _supervisor_lock
except ImportError:
    from recovery_locking import _runtime_guard, _supervisor_lock

def _launch_post_recovery(root: Path, launcher=None) -> tuple[bool, str]:
    command = [sys.executable, str(Path(root) / "app.py"), "--post-recovery"]
    try:
        launch = launcher or subprocess.Popen
        launch(command, cwd=str(root), close_fds=True)
        return True, ""
    except Exception as exc:
        return False, f"post_recovery_launch_failed:{type(exc).__name__}"


def _call_validator(validator, root: Path, journal: dict[str, Any], backup: Path) -> tuple[bool, str]:
    if validator is None:
        return validate_restored_install(root, journal, backup)
    try:
        return validator(root, journal, backup)
    except TypeError:
        return validator(root)


def _is_dependency_validation_error(detail: str) -> bool:
    text = str(detail or "")
    prefixes = (
        "dependency_",
        "critical_import_",
    )
    return text.startswith(prefixes)


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
    progress_hook=None,
    persist_waiting_state: bool = True,
) -> RecoveryResult:
    root = Path(root)
    try:
        initial = load_journal(root, backup_root=backup_root)
    except RecoveryJournalError as exc:
        return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "corrupt", _sanitize(exc), continue_startup=False)
    if initial is None or initial.get("state") == "cleared" or not initial.get("recovery_required"):
        return RecoveryResult(False, 0, "cleared", "", continue_startup=True)

    supervisor = None
    runtime = None
    factories = lock_factories or {}
    try:
        if not supervisor_already_held:
            supervisor = (factories.get("supervisor") or _supervisor_lock)()
            if not supervisor.acquire():
                return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, str(initial.get("state") or "recovery"), "recovery supervisor already active")
        runtime = (factories.get("runtime") or _runtime_guard)()
        if not runtime.acquire():
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, str(initial.get("state") or "recovery"), "runtime/recovery guard is busy")

        journal = load_journal(root, backup_root=backup_root)
        if journal is None or journal.get("state") == "cleared" or not journal.get("recovery_required"):
            return RecoveryResult(False, 0, "cleared", "", continue_startup=True)

        blocking, inspect_errors = evaluate_remaining_processes(journal, inspector=inspector)
        if journal.get("identity_verification_required"):
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, str(journal["state"]), "legacy process identity cannot be verified automatically")
        if blocking or inspect_errors:
            if persist_waiting_state and journal["state"] == "pip_termination_unconfirmed":
                journal = transition_journal(
                    root, journal, "waiting_for_processes", backup_root=backup_root,
                    errors=(list(journal.get("errors") or []) + list(inspect_errors))[-128:],
                )
            elif persist_waiting_state and inspect_errors:
                journal = append_journal_error(root, journal, inspect_errors[-1], backup_root=backup_root)
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "waiting_for_processes", "waiting for strong process identities")

        backup = resolve_backup(root, journal, backup_root=backup_root)
        state = str(journal["state"])

        if state == "dependency_repair_required":
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, state, "dependency repair required before Nova can start")

        if state == "update_validated":
            journal = transition_journal(root, journal, "cleared", backup_root=backup_root)
            state = "cleared"

        if state in ROLLBACK_ENTRY_STATES:
            if state != "rollback_in_progress":
                journal = transition_journal(
                    root, journal, "rollback_in_progress", backup_root=backup_root,
                    remaining_processes=[], files_rollback_attempted=True,
                )
            try:
                if restore_func is None:
                    restore_backup_idempotent(root, backup, backup_root=backup_root, progress_hook=progress_hook)
                else:
                    try:
                        restore_func(root, backup, progress_hook=progress_hook)
                    except TypeError:
                        restore_func(root, backup)
            except Exception as exc:
                journal = append_journal_error(root, journal, f"rollback_failed:{type(exc).__name__}:{exc}", backup_root=backup_root)
                return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "rollback_in_progress", "rollback failed")
            if progress_hook is not None:
                progress_hook("after_restore_before_validation", journal)
            journal = transition_journal(
                root, journal, "rollback_completed", backup_root=backup_root,
                files_rollback_attempted=True, files_rollback_ok=True,
            )
            state = "rollback_completed"

        if state == "rollback_completed":
            journal = transition_journal(root, journal, "rollback_validation_in_progress", backup_root=backup_root)
            state = "rollback_validation_in_progress"

        if state == "rollback_validation_in_progress":
            try:
                valid, detail = _call_validator(validator, root, journal, backup)
            except Exception as exc:
                valid, detail = False, f"validation_exception:{type(exc).__name__}"
            if not valid:
                if journal.get("dependencies_may_have_changed") and _is_dependency_validation_error(detail):
                    journal = transition_journal(
                        root, journal, "dependency_repair_required", backup_root=backup_root,
                        errors=(list(journal.get("errors") or []) + [_sanitize(detail, 500)])[-128:],
                    )
                    return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "dependency_repair_required", _sanitize(detail))
                journal = append_journal_error(root, journal, detail, backup_root=backup_root)
                return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "rollback_validation_in_progress", _sanitize(detail))
            journal = transition_journal(
                root, journal, "rollback_validation_completed", backup_root=backup_root,
                validation_detail=_sanitize(detail, 500),
            )
            state = "rollback_validation_completed"
            if progress_hook is not None:
                progress_hook("after_validation_before_clear", journal)

        if state == "rollback_validation_completed":
            journal = transition_journal(
                root, journal, "cleared", backup_root=backup_root,
                files_rollback_attempted=True, files_rollback_ok=True,
            )
            state = "cleared"

        if state != "cleared":
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, state, "recovery state requires manual inspection")

        if not launch_after_success:
            return RecoveryResult(False, 0, "cleared", "recovery completed", recovered=True, launched=False, continue_startup=True)

        runtime.release()
        runtime = None
        launched, launch_error = _launch_post_recovery(root, launcher=launcher)
        if not launched:
            try:
                current = load_journal(root, backup_root=backup_root)
                if current is not None and current.get("state") == "cleared":
                    transition_journal(
                        root, current, "rollback_validation_completed", backup_root=backup_root,
                        recovery_required=True,
                        errors=(list(current.get("errors") or []) + [launch_error])[-128:],
                    )
            except Exception:
                pass
            return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "rollback_validation_completed", launch_error, recovered=True, launched=False)
        return RecoveryResult(False, 0, "cleared", "recovery completed", recovered=True, launched=True, continue_startup=False)
    except RecoveryJournalError as exc:
        return RecoveryResult(True, RECOVERY_REQUIRED_EXIT_CODE, "invalid", _sanitize(exc))
    except Exception as exc:
        try:
            current = load_journal(root, backup_root=backup_root)
            if current is not None and current.get("state") != "cleared":
                append_journal_error(root, current, f"recovery_exception:{type(exc).__name__}:{exc}", backup_root=backup_root)
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
        f"Datos de recuperación: {safe_location}\n"
        "No borres update_recovery.json ni el backup.\n"
        "Diagnóstico: python updater/recovery_bootstrap.py --status\n"
        "Recuperación manual: python updater/recovery_bootstrap.py --recover"
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
    root: Path,
    *,
    inspector=None,
    backup_root: Path | None = None,
    launcher=None,
    lock_factories=None,
    show_notice: bool = True,
) -> RecoveryResult:
    result = recover_pending(
        root,
        supervisor_already_held=False,
        inspector=inspector,
        backup_root=backup_root,
        launcher=launcher,
        launch_after_success=True,
        lock_factories=lock_factories,
    )
    if not result.continue_startup and not (result.recovered and result.launched) and show_notice:
        _minimal_notice(Path(root), result)
    return result


def updater_recovery_gate(
    root: Path,
    *,
    supervisor_already_held: bool = True,
    inspector=None,
    backup_root: Path | None = None,
    launcher=None,
    lock_factories=None,
) -> RecoveryResult:
    result = recover_pending(
        root,
        supervisor_already_held=supervisor_already_held,
        inspector=inspector,
        backup_root=backup_root,
        launcher=launcher,
        launch_after_success=True,
        lock_factories=lock_factories,
        persist_waiting_state=False,
    )
    if not result.pending and not result.recovered and result.continue_startup:
        return RecoveryResult(False, 0, "", "no recovery pending", continue_startup=True)
    return RecoveryResult(
        True,
        RECOVERY_REQUIRED_EXIT_CODE,
        result.state,
        result.detail or "recovery handled before update",
        recovered=result.recovered,
        launched=result.launched,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Nova persistent recovery bootstrap")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--root", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
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
                "dependencies_may_have_changed", "dependency_snapshot_path",
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
