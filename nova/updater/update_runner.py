from __future__ import annotations

"""Resident updater supervisor.

The supervisor owns the update mutex and runtime guard.  The internal engine
runs in this process and may only leave a mutating attempt at a durable active
state.  Validated attempts are finished through the shared recovery handoff;
there is no direct supervisor launch after a validated transaction.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
from pathlib import Path
import time
from typing import Any

try:
    from . import update_runner_legacy as _legacy
except ImportError:
    import update_runner_legacy as _legacy

for _name in dir(_legacy):
    if _name.startswith("__"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_legacy, _name)

SUPERVISOR_ALREADY_RUNNING_CODE = 5
PIP_TERMINATION_UNCONFIRMED_CODE = 6
RECOVERY_REQUIRED_CODE = 7
_VALIDATED_HANDOFF_STATES = {"update_validated", "rollback_validation_completed"}


def run_update(root: Path, log: Path):
    """Execute the import-only engine in the already-guarded supervisor process.

    Returns ``(exit_code, error, journal)`` in production. ``main`` also accepts
    the historic two-tuple from injected tests so no-journal compatibility paths
    remain testable without manufacturing a transaction.
    """
    try:
        try:
            from . import resident_update_engine
        except ImportError:
            import resident_update_engine
        with open(log, "w", encoding="utf-8", errors="replace") as stream:
            stream.write("Nova Update Runner · crash-durable validated handoff\n")
            stream.write("Motor interno: import updater.resident_update_engine (same process)\n\n")
            stream.flush()
            with redirect_stdout(stream), redirect_stderr(stream):
                result = resident_update_engine.run_supervised_update(root)
        if hasattr(result, "exit_code"):
            return int(result.exit_code), "", getattr(result, "journal", None)
        return int(result), "", None
    except Exception as exc:
        return 2, f"internal_engine_exception:{type(exc).__name__}:{exc}", None


def _normalize_run_update_result(value) -> tuple[int, str, dict[str, Any] | None]:
    if isinstance(value, tuple):
        if len(value) == 3:
            rc, error, journal = value
            return int(rc), str(error or ""), journal if isinstance(journal, dict) else None
        if len(value) == 2:
            rc, error = value
            return int(rc), str(error or ""), None
    if hasattr(value, "exit_code"):
        return int(value.exit_code), str(getattr(value, "detail", "") or ""), getattr(value, "journal", None)
    return int(value), "", None


def _recovery_gate(root: Path):
    try:
        from .recovery_bootstrap import updater_recovery_gate
    except ImportError:
        from recovery_bootstrap import updater_recovery_gate
    return updater_recovery_gate(root, supervisor_already_held=True)


def _strong_remaining_pids(root: Path) -> list[int]:
    try:
        data = json.loads((root / "data" / "update_recovery.json").read_text(encoding="utf-8"))
        rows = data.get("remaining_processes") or []
        return sorted({
            int(row.get("pid") or 0)
            for row in rows
            if isinstance(row, dict) and int(row.get("pid") or 0) > 0
        })[:32]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _load_durable_journal(root: Path) -> tuple[dict[str, Any] | None, str]:
    path = Path(root) / "data" / "update_recovery.json"
    if not path.exists():
        return None, ""
    try:
        try:
            from .recovery_state import load_journal
        except ImportError:
            from recovery_state import load_journal
        journal = load_journal(root)
    except Exception as exc:
        return None, f"journal_unverifiable:{type(exc).__name__}"
    if journal is None:
        return None, ""
    return journal, ""


def _journal_after_engine(root: Path) -> tuple[bool, str, str]:
    """Compatibility diagnostic based on durable state, never just engine rc."""
    journal, error = _load_durable_journal(root)
    if error:
        return True, "corrupt", error
    if journal is None:
        return False, "", "no_journal"
    state = str(journal.get("state") or "unknown")
    active = bool(journal.get("recovery_required")) and state != "cleared"
    return active, state, "active_recovery" if active else "cleared"


def _append_log_best_effort(log: Path | None, text: str) -> None:
    if log is None:
        return
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8", errors="replace") as stream:
            stream.write(str(text))
    except OSError:
        pass


def _read_version_best_effort(root: Path, fallback: str, log: Path, label: str) -> str:
    try:
        return read_version(root)
    except Exception as exc:
        _append_log_best_effort(log, f"\n[WARN VERSION {label}] {type(exc).__name__}: {exc}\n")
        return fallback


def _write_status_best_effort(root: Path, **kwargs) -> None:
    try:
        write_status(root, **kwargs)
    except Exception as exc:
        log = kwargs.get("log")
        if log is not None:
            _append_log_best_effort(Path(log), f"\n[WARN ESTADO] {type(exc).__name__}: {exc}\n")


def _launch_recovery_once(root: Path, log: Path) -> tuple[bool, str]:
    """Legacy/no-journal relaunch only; never used for a validated transaction."""
    try:
        launched, detail = launch_nova(root)
    except Exception as exc:
        launched, detail = False, f"{type(exc).__name__}: {exc}"
    if not launched:
        _append_log_best_effort(log, "\n[ERROR REINICIO] " + str(detail or "fallo desconocido") + "\n")
    return bool(launched), str(detail or "")


def _launch_validated_handoff(
    root: Path,
    journal: dict[str, Any],
    mode: str,
    *,
    coordination=None,
    release_guard=None,
    launcher=None,
    crash_hook=None,
    timeout_seconds: float = 20.0,
    backup_root: Path | None = None,
):
    """Authoritative production wrapper for one validated handoff.

    The supplied journal is not trusted blindly: the shared implementation
    rereads under the journal CAS contract before spawning anything. Production
    passes coordination so its guard is consumed exactly once.
    """
    try:
        from .recovery_handoff import perform_validated_handoff
    except ImportError:
        from recovery_handoff import perform_validated_handoff

    if release_guard is None:
        if coordination is not None:
            release_guard = coordination.release_guard
        else:
            release_guard = lambda: None
    return perform_validated_handoff(
        root,
        journal,
        mode,
        release_guard=release_guard,
        helper_launcher=launcher,
        crash_hook=crash_hook,
        timeout_seconds=timeout_seconds,
        backup_root=backup_root,
    )


def _release_coordination_if_owned(coordination, log: Path | None) -> None:
    if coordination is None or getattr(coordination, "guard", None) is None:
        return
    try:
        coordination.release_guard()
    except Exception as exc:
        _append_log_best_effort(log, f"\n[WARN GUARD] {type(exc).__name__}: {exc}\n")


def main(argv=None, *, supervisor_lock_factory=None) -> int:
    parser = argparse.ArgumentParser(description="Supervisa una actualización de Nova y relanza la aplicación.")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)
    root = nova_root()

    try:
        supervisor_mutex = _acquire_supervisor_mutex(root, supervisor_lock_factory)
    except Exception as exc:
        print(f"[ERROR] No pude adquirir el mutex del supervisor: {type(exc).__name__}: {exc}")
        return 4
    if supervisor_mutex is None:
        print("Actualización ya en curso.")
        return SUPERVISOR_ALREADY_RUNNING_CODE

    log: Path | None = None
    coordination = None
    try:
        try:
            recovery = _recovery_gate(root)
        except Exception as exc:
            print(f"Recuperación requerida o no verificable: {type(exc).__name__}")
            return RECOVERY_REQUIRED_CODE
        if recovery.pending or recovery.recovered or not recovery.continue_startup:
            print("Recuperación requerida o procesada antes de actualizar. Reintenta después de recuperar Nova.")
            return RECOVERY_REQUIRED_CODE

        logs = root / "data" / "updater_logs"
        log = logs / ("update_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
        try:
            logs.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        before = _read_version_best_effort(root, "0.0.0", log, "ANTES")
        try:
            coordination = coordinate_runtime_shutdown(root, args.wait_seconds, expected_pid=args.parent_pid)
        except Exception as exc:
            coordination = ShutdownCoordination(False, f"coordination_exception:{type(exc).__name__}")

        if not coordination.ok:
            if coordination.process_terminated:
                error = (
                    "Nova terminó, pero la coordinación no obtuvo un guard verificable; "
                    "no se modificaron archivos. " + coordination.error
                )
            else:
                error = "Nova no terminó de forma verificable; no se modificaron archivos. " + coordination.error
            _write_status_best_effort(
                root, ok=False, before=before, after=before, log=log,
                error=error, state="coordination_failed",
            )
            _release_coordination_if_owned(coordination, log)
            if coordination.process_terminated:
                _launch_recovery_once(root, log)
            else:
                try:
                    _show_surviving_runtime(root, coordination)
                except Exception as exc:
                    _append_log_best_effort(log, f"\n[WARN SHOW] {type(exc).__name__}: {exc}\n")
            return 4

        rc = 2
        final_rc = 2
        runner_error = ""
        engine_journal: dict[str, Any] | None = None
        durable_journal: dict[str, Any] | None = None
        durable_error = ""
        state = "update_failed"
        error = ""
        remaining_pids: list[int] = []
        legacy_launch = False
        handoff_used = False
        handoff_ok = False

        try:
            try:
                rc, runner_error, engine_journal = _normalize_run_update_result(run_update(root, log))
            except Exception as exc:
                rc = 2
                runner_error = f"run_update inesperado: {type(exc).__name__}: {exc}"
                engine_journal = None

            durable_journal, durable_error = _load_durable_journal(root)
            if durable_error:
                final_rc = RECOVERY_REQUIRED_CODE
                state = "recovery_required_or_in_progress"
                error = f"El journal posterior al motor no puede verificarse: {durable_error}."
            else:
                candidate = engine_journal if isinstance(engine_journal, dict) else durable_journal
                candidate_state = str((candidate or {}).get("state") or "")

                if candidate_state in _VALIDATED_HANDOFF_STATES:
                    if durable_journal is None:
                        final_rc = RECOVERY_REQUIRED_CODE
                        state = "recovery_required_or_in_progress"
                        error = "El motor devolvió un intento validado, pero el journal durable desapareció."
                    else:
                        handoff_used = True
                        mode = "post-update" if candidate_state == "update_validated" else "post-recovery"
                        try:
                            handoff = _launch_validated_handoff(
                                root,
                                candidate,
                                mode,
                                coordination=coordination,
                                timeout_seconds=max(1.0, min(float(args.wait_seconds), 60.0)),
                            )
                        except Exception as exc:
                            handoff = None
                            error = f"handoff_exception:{type(exc).__name__}:{exc}"
                        if handoff is not None and bool(getattr(handoff, "ok", False)):
                            handoff_ok = True
                            final_rc = int(rc)
                            state = "completed" if final_rc == 0 else "recovered_after_update_failure"
                        else:
                            final_rc = RECOVERY_REQUIRED_CODE
                            state = str(getattr(handoff, "state", candidate_state) or candidate_state)
                            detail = str(getattr(handoff, "detail", "") or error or "validated_handoff_failed")
                            error = f"Handoff validado incompleto; se conserva cuarentena. {detail}"
                elif durable_journal is not None and bool(durable_journal.get("recovery_required")):
                    state = str(durable_journal.get("state") or "recovery_required_or_in_progress")
                    remaining_pids = _strong_remaining_pids(root)
                    if int(rc) == PIP_TERMINATION_UNCONFIRMED_CODE:
                        final_rc = PIP_TERMINATION_UNCONFIRMED_CODE
                        error = "La terminación de pip no pudo confirmarse; cuarentena persistente activa."
                    else:
                        final_rc = RECOVERY_REQUIRED_CODE
                        error = f"La instalación conserva recuperación activa ({state}); Nova no será relanzada."
                elif durable_journal is not None and str(durable_journal.get("state") or "") == "cleared":
                    final_rc = int(rc)
                    state = "completed" if final_rc == 0 else "update_failed"
                    legacy_launch = True
                else:
                    final_rc = int(rc)
                    state = "completed" if final_rc == 0 else "update_failed"
                    legacy_launch = int(rc) != PIP_TERMINATION_UNCONFIRMED_CODE

            if runner_error and not error:
                error = str(runner_error)
            elif final_rc not in (0, PIP_TERMINATION_UNCONFIRMED_CODE) and not error:
                error = f"El updater terminó con código {final_rc}. Revisa {log}."

            after = _read_version_best_effort(root, before, log, "DESPUÉS")
            _write_status_best_effort(
                root,
                ok=(final_rc == 0 and (handoff_ok or legacy_launch)),
                before=before,
                after=after,
                log=log,
                error=error,
                state=state,
                remaining_pids=remaining_pids,
            )
        finally:
            _release_coordination_if_owned(coordination, log)

        if handoff_used:
            if handoff_ok:
                return final_rc
            return RECOVERY_REQUIRED_CODE

        if not legacy_launch:
            return final_rc

        launched, _launch_detail = _launch_recovery_once(root, log)
        if final_rc == 0:
            return 0 if launched else 3
        return final_rc or 2
    finally:
        _release_supervisor_mutex_best_effort(supervisor_mutex, log)


if __name__ == "__main__":
    raise SystemExit(main())
