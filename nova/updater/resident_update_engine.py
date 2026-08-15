from __future__ import annotations

"""Import-only resident update transaction orchestrator.

The low-level dependency launcher lives in resident_engine_core; all installation
mutation remains in this module and is reachable only through the supervisor.
The engine never clears a validated journal: the resident supervisor owns the
final validated launch handoff.
"""

import os
from pathlib import Path
from typing import Any

try:
    from .resident_engine_core import (
        DIRECT_EXECUTION_BLOCKED_EXIT_CODE, PIP_TERMINATION_UNCONFIRMED_EXIT_CODE,
        RECOVERY_REQUIRED_EXIT_CODE, CrashHook, DependencyInstallError,
        PipTerminationUnconfirmedError, RecoveryRequiredError, _base_root,
        _ensure_no_recovery_pending, _hook, _install_requirements, _process_identity,
        base,
    )
    from .recovery_state import (
        RecoveryJournalError, append_journal_error, capture_dependency_snapshot,
        create_transaction_journal, load_journal, prepare_stable_recovery_runtime,
        restore_backup_idempotent, transition_journal, validate_backup_path,
        validate_restored_install,
    )
except ImportError:
    from resident_engine_core import (
        DIRECT_EXECUTION_BLOCKED_EXIT_CODE, PIP_TERMINATION_UNCONFIRMED_EXIT_CODE,
        RECOVERY_REQUIRED_EXIT_CODE, CrashHook, DependencyInstallError,
        PipTerminationUnconfirmedError, RecoveryRequiredError, _base_root,
        _ensure_no_recovery_pending, _hook, _install_requirements, _process_identity,
        base,
    )
    from recovery_state import (
        RecoveryJournalError, append_journal_error, capture_dependency_snapshot,
        create_transaction_journal, load_journal, prepare_stable_recovery_runtime,
        restore_backup_idempotent, transition_journal, validate_backup_path,
        validate_restored_install,
    )

VALIDATED_HANDOFF_STATES = {"update_validated", "rollback_validation_completed"}


class SupervisedUpdateResult(int):
    """Integer-compatible engine result carrying the exact durable journal snapshot."""

    def __new__(
        cls,
        exit_code: int,
        journal: dict[str, Any] | None = None,
        detail: str = "",
    ):
        obj = int.__new__(cls, int(exit_code))
        obj.exit_code = int(exit_code)
        obj.journal = journal
        obj.detail = str(detail or "")
        return obj


def _apply_transaction(stage: Path, manifest: dict[str, list[str]], *, crash_hook: CrashHook | None = None) -> None:
    lists = base._validated_manifest_lists(manifest)
    replace_rows = list(lists["modified_existing"] + lists["created_new"])
    delete_rows = list(lists["deleted_existing"])
    total = len(replace_rows) + len(delete_rows)
    midpoint = max(1, (total + 1) // 2)
    completed = 0
    first_replaced = False
    midpoint_fired = False
    for rel in replace_rows:
        rel_path = base.safe_rel(rel)
        base._atomic_replace_from(Path(stage) / rel_path, Path(base.ROOT) / rel_path)
        completed += 1
        if not first_replaced:
            first_replaced = True
            _hook(crash_hook, "after_first_file", rel=rel, completed=completed, total=total)
        if not midpoint_fired and completed >= midpoint:
            midpoint_fired = True
            _hook(crash_hook, "mid_apply", rel=rel, completed=completed, total=total)
    for rel in delete_rows:
        target = Path(base.ROOT) / base.safe_rel(rel)
        if target.exists() and not target.is_file():
            raise RuntimeError(f"No se puede eliminar destino no-archivo: {rel}")
        if target.is_file():
            target.unlink()
        completed += 1
        if not midpoint_fired and completed >= midpoint:
            midpoint_fired = True
            _hook(crash_hook, "mid_apply", rel=rel, completed=completed, total=total)


def _dependency_validation_error(detail: str) -> bool:
    return str(detail or "").startswith(("dependency_", "critical_import_"))


def _rollback_after_failure(
    root: Path,
    backup: Path,
    journal: dict[str, Any],
    update_error: BaseException,
    *,
    backup_root: Path | None,
    dependencies_may_have_changed: bool,
    crash_hook: CrashHook | None,
) -> dict[str, Any]:
    current = journal
    if current["state"] != "rollback_in_progress":
        current = transition_journal(
            root, current, "rollback_in_progress", backup_root=backup_root,
            dependencies_may_have_changed=bool(dependencies_may_have_changed),
            files_rollback_attempted=True,
            remaining_processes=[],
        )
    _hook(crash_hook, "after_failure_before_rollback", state=current["state"])
    restore_count = {"n": 0}

    def progress(kind, count, rel):
        restore_count["n"] = count
        _hook(crash_hook, "mid_rollback", kind=kind, count=count, rel=rel)

    try:
        restore_backup_idempotent(root, backup, backup_root=backup_root, progress_hook=progress)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        try:
            append_journal_error(root, current, f"rollback_failed:{type(exc).__name__}", backup_root=backup_root)
        except Exception:
            pass
        raise RecoveryRequiredError(f"rollback incompleto; backup preservado: {type(exc).__name__}") from update_error

    _hook(crash_hook, "after_restore_before_validation", operations=restore_count["n"])
    current = transition_journal(
        root, current, "rollback_completed", backup_root=backup_root,
        files_rollback_attempted=True, files_rollback_ok=True,
    )
    current = transition_journal(root, current, "rollback_validation_in_progress", backup_root=backup_root)
    valid, detail = validate_restored_install(root, current, backup)
    if not valid:
        if current.get("dependencies_may_have_changed") and _dependency_validation_error(detail):
            transition_journal(
                root, current, "dependency_repair_required", backup_root=backup_root,
                errors=(list(current.get("errors") or []) + [str(detail)])[-128:],
            )
            raise RecoveryRequiredError(f"dependency_repair_required:{detail}") from update_error
        append_journal_error(root, current, detail, backup_root=backup_root)
        raise RecoveryRequiredError(f"rollback validation failed:{detail}") from update_error
    current = transition_journal(
        root, current, "rollback_validation_completed", backup_root=backup_root,
        validation_detail=str(detail)[:500],
    )
    _hook(crash_hook, "after_validation_before_clear", state=current["state"])
    return current


def execute_transaction(
    stage: Path,
    new_files: list[str],
    previous: set[str],
    tag: str,
    old_version: str,
    new_version: str,
    *,
    backup_root: Path | None = None,
    pip_timeout_seconds=None,
    crash_hook: CrashHook | None = None,
):
    root = Path(base.ROOT)
    _ensure_no_recovery_pending(root)
    manifest = base.build_transaction(stage, new_files, previous)
    backup = base.create_backup(manifest, old_version, new_version, backup_root=backup_root)
    backup = validate_backup_path(root, backup, backup_root=backup_root)
    print(f"Backup: {backup}")

    journal = create_transaction_journal(root, backup, backup_root=backup_root)
    _hook(crash_hook, "after_journal_before_files", attempt_id=journal["attempt_id"], generation=journal["generation"])

    requirements_changed = "requirements.txt" in set(manifest["modified_existing"] + manifest["created_new"])
    try:
        prepare_stable_recovery_runtime(root)

        if requirements_changed:
            snapshot_rel, snapshot_sha = capture_dependency_snapshot(root, backup, backup_root=backup_root)
            journal = transition_journal(
                root, journal, "transaction_prepared", backup_root=backup_root,
                dependency_snapshot_path=snapshot_rel,
                dependency_snapshot_sha256=snapshot_sha,
            )

        journal = transition_journal(
            root, journal, "files_applying", backup_root=backup_root,
            files_may_have_changed=True,
        )
        _apply_transaction(stage, manifest, crash_hook=crash_hook)
        base.write_managed(new_files, tag)
        journal = transition_journal(root, journal, "files_applied", backup_root=backup_root)
        _hook(crash_hook, "after_files_applied", state=journal["state"])

        if requirements_changed:
            journal = transition_journal(
                root, journal, "dependencies_starting", backup_root=backup_root,
                dependencies_may_have_changed=True,
            )
            _hook(crash_hook, "after_dependencies_may_change_before_pip", state=journal["state"])

            def on_started(proc):
                nonlocal journal
                identities, identity_complete = _process_identity(proc)
                journal = transition_journal(
                    root, journal, "dependencies_running", backup_root=backup_root,
                    remaining_processes=identities,
                    identity_verification_required=(os.name != "nt" and not identity_complete),
                )
                _hook(crash_hook, "dependencies_running", pid=int(getattr(proc, "pid", 0) or 0), state=journal["state"])

            _install_requirements(timeout_seconds=pip_timeout_seconds, on_started=on_started)
            journal = transition_journal(
                root, journal, "update_validation_in_progress", backup_root=backup_root,
                remaining_processes=[], identity_verification_required=False,
            )
        else:
            journal = transition_journal(root, journal, "update_validation_in_progress", backup_root=backup_root)

        ok, detail = base.validate_install()
        if not ok:
            raise RuntimeError(detail)
        journal = transition_journal(
            root, journal, "update_validated", backup_root=backup_root,
            validation_detail=str(detail)[:500],
        )
        _hook(crash_hook, "after_update_validated_before_clear", state=journal["state"])
        print(detail)
        return backup, manifest, journal

    except PipTerminationUnconfirmedError as update_error:
        result = update_error.result
        try:
            journal = load_journal(root, backup_root=backup_root) or journal
            if journal["state"] != "pip_termination_unconfirmed":
                journal = transition_journal(
                    root, journal, "pip_termination_unconfirmed", backup_root=backup_root,
                    dependencies_may_have_changed=True,
                    remaining_processes=list(result.remaining_processes or []),
                    identity_verification_required=not bool(result.identity_inspection_complete),
                    errors=(list(journal.get("errors") or []) + list(result.termination_errors or []))[-128:],
                )
        except Exception as state_error:
            raise RecoveryRequiredError(f"no se pudo persistir cuarentena de pip:{type(state_error).__name__}") from update_error
        raise
    except BaseException as update_error:
        if isinstance(update_error, (KeyboardInterrupt, SystemExit)):
            raise
        current = load_journal(root, backup_root=backup_root)
        if current is None or current.get("state") == "cleared":
            raise RecoveryRequiredError("journal activo ausente durante una transacción mutante") from update_error
        dependencies_changed = bool(current.get("dependencies_may_have_changed"))
        if isinstance(update_error, DependencyInstallError) and not update_error.dependency_started:
            dependencies_changed = False
        _rollback_after_failure(
            root, backup, current, update_error,
            backup_root=backup_root,
            dependencies_may_have_changed=dependencies_changed,
            crash_hook=crash_hook,
        )
        raise


def _safe_current_journal(root: Path) -> dict[str, Any] | None:
    try:
        journal = load_journal(root)
    except RecoveryJournalError:
        return None
    if journal is None or journal.get("state") == "cleared" or not journal.get("recovery_required"):
        return None
    return journal


def run_supervised_update(
    root: Path | None = None,
    *,
    pip_timeout_seconds=None,
    crash_hook: CrashHook | None = None,
) -> SupervisedUpdateResult:
    """Run one update while the caller owns supervisor + runtime guards."""
    target_root = Path(root or getattr(base, "ROOT", Path(__file__).resolve().parents[1])).resolve()
    last_journal: dict[str, Any] | None = None
    with _base_root(target_root):
        try:
            _ensure_no_recovery_pending(target_root)
            cfg = base.load_config()
            current = base.version_text()
            release = base.get_release(cfg)
            latest = str(release.get("tag_name", "")).lstrip("vV")
            notes = (release.get("body") or "").strip()
            print("=" * 58)
            print("NOVA UPDATER 2.4 - crash durable resident transaction")
            print("=" * 58)
            print(f"Repositorio : {cfg['repository']}")
            print(f"Canal       : {cfg.get('channel', 'stable')}")
            print(f"Instalada   : {current}")
            print(f"Disponible  : {latest}")
            if base.version_key(latest) <= base.version_key(current):
                print("\nNova ya está actualizada.")
                return SupervisedUpdateResult(0, None, "up_to_date")
            if notes:
                print("\nCambios:\n" + notes[:2500])

            old_execute = base.execute_transaction

            def guarded_execute(stage, new_files, previous, tag, old_version, new_version, **kwargs):
                nonlocal last_journal
                try:
                    backup, manifest, journal = execute_transaction(
                        stage, new_files, previous, tag, old_version, new_version,
                        backup_root=kwargs.get("backup_root"),
                        pip_timeout_seconds=pip_timeout_seconds if pip_timeout_seconds is not None else kwargs.get("pip_timeout_seconds"),
                        crash_hook=crash_hook,
                    )
                except BaseException:
                    last_journal = _safe_current_journal(target_root)
                    raise
                last_journal = journal
                return backup, manifest

            base.execute_transaction = guarded_execute
            try:
                base.sync_release(cfg, release)
            finally:
                base.execute_transaction = old_execute
            print("\n" + "=" * 58)
            print(f"NOVA {latest} INSTALADA DESDE GITHUB")
            print("=" * 58)
            return SupervisedUpdateResult(0, last_journal, "update_validated")
        except PipTerminationUnconfirmedError as exc:
            print(f"[ERROR] {exc}")
            return SupervisedUpdateResult(
                PIP_TERMINATION_UNCONFIRMED_EXIT_CODE,
                last_journal or _safe_current_journal(target_root),
                str(exc),
            )
        except RecoveryRequiredError as exc:
            print(f"[RECOVERY REQUIRED] {exc}")
            return SupervisedUpdateResult(
                RECOVERY_REQUIRED_EXIT_CODE,
                last_journal or _safe_current_journal(target_root),
                str(exc),
            )
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}")
            return SupervisedUpdateResult(2, last_journal or _safe_current_journal(target_root), f"{type(exc).__name__}:{exc}")


def main(argv=None) -> int:
    print("[ERROR] resident_update_engine es import-only; usa updater/update_runner.py.")
    return DIRECT_EXECUTION_BLOCKED_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
