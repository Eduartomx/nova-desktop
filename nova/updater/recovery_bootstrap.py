from __future__ import annotations

"""Fail-closed façade for Nova's stdlib-only recovery coordinator.

The validated coordinator implementation lives in ``recovery_bootstrap_legacy``.
This façade preserves its public contract while persisting the explicit
``waiting_for_processes`` state during startup/recovery and re-establishing
quarantine if the final post-recovery launch fails. A new update request only
observes an existing quarantine; it never mutates that journal while blocked.
"""

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

try:
    from . import recovery_bootstrap_legacy as _legacy
    from .recovery_state import (
        RECOVERY_REQUIRED_EXIT_CODE,
        RecoveryJournalError,
        RecoveryResult,
        _sanitize,
        evaluate_remaining_processes,
        load_journal,
        transition_journal,
    )
except ImportError:
    import recovery_bootstrap_legacy as _legacy
    from recovery_state import (
        RECOVERY_REQUIRED_EXIT_CODE,
        RecoveryJournalError,
        RecoveryResult,
        _sanitize,
        evaluate_remaining_processes,
        load_journal,
        transition_journal,
    )

for _name in dir(_legacy):
    if _name.startswith("__"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_legacy, _name)


def _persist_waiting_state(root: Path, *, backup_root: Path | None, inspector=None) -> None:
    try:
        journal = load_journal(root, backup_root=backup_root)
    except RecoveryJournalError:
        return
    if journal is None or not journal.get("recovery_required"):
        return
    if str(journal.get("state") or "") != "pip_termination_unconfirmed":
        return
    blocking, errors = evaluate_remaining_processes(journal, inspector=inspector)
    if not (blocking or errors or journal.get("identity_verification_required")):
        return
    updates: dict[str, Any] = {}
    if errors:
        merged = list(journal.get("errors") or []) + [_sanitize(value, 300) for value in errors]
        updates["errors"] = merged[-128:]
    transition_journal(root, journal, "waiting_for_processes", **updates)


def _restore_launch_quarantine(root: Path, *, backup_root: Path | None, error: str) -> None:
    try:
        journal = load_journal(root, backup_root=backup_root)
        if journal is None:
            return
        errors = list(journal.get("errors") or [])
        errors.append(_sanitize(error or "post_recovery_launch_failed", 400))
        # validation_completed means rollback and validation are already safe;
        # the next recovery attempt skips them and retries only the final launch.
        transition_journal(
            root,
            journal,
            "validation_completed",
            recovery_required=True,
            errors=errors[-128:],
        )
    except Exception:
        # If this write itself fails, the original recovery result still returns
        # code 7. Startup will fail closed on the remaining/corrupt journal.
        pass


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
    persist_waiting_state: bool = True,
) -> RecoveryResult:
    root = Path(root)
    if persist_waiting_state:
        _persist_waiting_state(root, backup_root=backup_root, inspector=inspector)

    wrapped_launcher = launcher
    launch_failure: list[str] = []
    if launch_after_success:
        target_launcher = launcher or subprocess.Popen

        def wrapped_launcher(command, **kwargs):
            try:
                return target_launcher(command, **kwargs)
            except Exception as exc:
                detail = f"post_recovery_launch_failed:{type(exc).__name__}"
                launch_failure.append(detail)
                _restore_launch_quarantine(root, backup_root=backup_root, error=detail)
                raise

    result = _legacy.recover_pending(
        root,
        supervisor_already_held=supervisor_already_held,
        inspector=inspector,
        backup_root=backup_root,
        restore_func=restore_func,
        validator=validator,
        launcher=wrapped_launcher,
        launch_after_success=launch_after_success,
        lock_factories=lock_factories,
    )
    if result.recovered and not result.launched and launch_after_success:
        detail = launch_failure[-1] if launch_failure else (result.detail or "post_recovery_launch_failed")
        _restore_launch_quarantine(root, backup_root=backup_root, error=detail)
        return RecoveryResult(
            True,
            RECOVERY_REQUIRED_EXIT_CODE,
            "validation_completed",
            _sanitize(detail, 400),
            recovered=True,
            launched=False,
            continue_startup=False,
        )
    return result


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
        persist_waiting_state=True,
    )
    if not result.continue_startup and not (result.recovered and result.launched) and show_notice:
        _legacy._minimal_notice(Path(root), result)
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
    # A new update is read-only with respect to an existing quarantine. It may
    # perform recovery once all identities are safe, but while blocked it must
    # not rewrite the owner's journal or backup.
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
