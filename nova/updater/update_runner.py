from __future__ import annotations

"""Compatibility wrapper for the resident updater supervisor.

The previously validated coordination primitives remain in
``update_runner_legacy``. This module only adds the persistent recovery gate,
codes 6/7 semantics, and routes the actual update through the hardened resident
transaction engine.
"""

import json
import subprocess
import time
from pathlib import Path

try:
    from . import update_runner_legacy as _legacy
except ImportError:  # direct script execution from nova/updater
    import update_runner_legacy as _legacy

# Preserve every existing helper (including private names) so tests and callers
# keep the same module contract while main() below uses the hardened flow.
for _name in dir(_legacy):
    if _name.startswith("__"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_legacy, _name)

SUPERVISOR_ALREADY_RUNNING_CODE = 5
PIP_TERMINATION_UNCONFIRMED_CODE = 6
RECOVERY_REQUIRED_CODE = 7


def run_update(root: Path, log: Path) -> tuple[int, str]:
    py = console_python(root)
    updater = root / "updater" / "resident_update_engine.py"
    if not updater.exists():
        return 2, f"No existe {updater}"
    cmd = [str(py), str(updater), "--yes"]
    try:
        with open(log, "w", encoding="utf-8", errors="replace") as stream:
            stream.write("Nova Update Runner · recovery hardened\n")
            stream.write("Motor interno: resident_update_engine.py\n\n")
            stream.flush()
            proc = subprocess.run(cmd, cwd=str(root), stdout=stream, stderr=subprocess.STDOUT, text=True)
        return int(proc.returncode), ""
    except Exception as exc:
        return 2, str(exc)


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
        return sorted({int(row.get("pid") or 0) for row in rows if isinstance(row, dict) and int(row.get("pid") or 0) > 0})[:32]
    except Exception:
        return []


def _recovery_state_exists(root: Path) -> bool:
    """Code 7 is fail-closed only when a durable journal actually exists.

    This preserves compatibility with older/custom updater return codes while
    the hardened engine always persists update_recovery.json before returning 7.
    A corrupt journal also counts as recovery state and therefore blocks launch.
    """
    try:
        return (Path(root) / "data" / "update_recovery.json").is_file()
    except Exception:
        return True


def _append_log_best_effort(log: Path, text: str) -> None:
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8", errors="replace") as stream:
            stream.write(str(text))
    except Exception:
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
    try:
        launched, detail = launch_nova(root)
    except Exception as exc:
        launched, detail = False, f"{type(exc).__name__}: {exc}"
    if not launched:
        _append_log_best_effort(log, "\n[ERROR REINICIO] " + str(detail or "fallo desconocido") + "\n")
    return bool(launched), str(detail or "")


def main(argv=None, *, supervisor_lock_factory=None) -> int:
    parser = argparse.ArgumentParser(description="Supervisa una actualización de Nova y relanza la aplicación.")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)
    root = nova_root()

    # Strict order for both normal update and recovery:
    # supervisor mutex -> recovery/runtime guard -> update/rollback/validation
    # -> release runtime guard -> launch (only when safe) -> supervisor release.
    try:
        supervisor_mutex = _acquire_supervisor_mutex(root, supervisor_lock_factory)
    except Exception as exc:
        print(f"[ERROR] No pude adquirir el mutex del supervisor: {type(exc).__name__}: {exc}")
        return 4
    if supervisor_mutex is None:
        print("Actualización ya en curso.")
        return SUPERVISOR_ALREADY_RUNNING_CODE

    log: Path | None = None
    try:
        # Quarantine is checked while the unique supervisor mutex is already
        # held and before reading/coordinating the running Nova. A recovery is
        # performed/delegated instead of continuing this update request.
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
        except Exception:
            pass

        before = _read_version_best_effort(root, "0.0.0", log, "ANTES")
        try:
            coordination = coordinate_runtime_shutdown(root, args.wait_seconds, expected_pid=args.parent_pid)
        except Exception as exc:
            coordination = ShutdownCoordination(False, f"coordination_exception:{type(exc).__name__}")

        if not coordination.ok:
            if coordination.process_terminated:
                error = "Nova terminó, pero la coordinación no obtuvo un guard verificable; no se modificaron archivos. " + coordination.error
            else:
                error = "Nova no terminó de forma verificable; no se modificaron archivos. " + coordination.error
            _write_status_best_effort(root, ok=False, before=before, after=before, log=log, error=error, state="coordination_failed")
            _release_coordination_guard_best_effort(coordination, log)
            if coordination.process_terminated:
                _launch_recovery_once(root, log)
            else:
                try:
                    _show_surviving_runtime(root, coordination)
                except Exception as exc:
                    _append_log_best_effort(log, f"\n[WARN SHOW] {type(exc).__name__}: {exc}\n")
            return 4

        rc = 2
        ok = False
        after = before
        error = ""
        runner_error = ""
        launched = False
        no_launch = False
        state = "update_failed"
        remaining_pids: list[int] = []

        try:
            try:
                rc, runner_error = run_update(root, log)
                rc = int(rc)
            except Exception as exc:
                rc = 2
                runner_error = f"run_update inesperado: {type(exc).__name__}: {exc}"

            durable_recovery = _recovery_state_exists(root)
            no_launch = rc == PIP_TERMINATION_UNCONFIRMED_CODE or (
                rc == RECOVERY_REQUIRED_CODE and durable_recovery
            )
            ok = rc == 0
            state = "completed" if ok else "update_failed"
            if rc == PIP_TERMINATION_UNCONFIRMED_CODE:
                state = "pip_termination_unconfirmed"
                remaining_pids = _strong_remaining_pids(root)
                error = "La terminación de pip no pudo confirmarse; cuarentena persistente activa."
            elif rc == RECOVERY_REQUIRED_CODE and durable_recovery:
                state = "recovery_required_or_in_progress"
                remaining_pids = _strong_remaining_pids(root)
                error = "La instalación requiere recuperación antes de iniciar Nova o aplicar otra actualización."
            elif runner_error:
                error = str(runner_error)
            elif not ok:
                error = f"El updater terminó con código {rc}. Revisa {log}."

            after = _read_version_best_effort(root, before, log, "DESPUÉS")
            _write_status_best_effort(
                root, ok=ok, before=before, after=after, log=log,
                error=error, state=state, remaining_pids=remaining_pids,
            )
        finally:
            _release_coordination_guard_best_effort(coordination, log)
            if not no_launch:
                launched, _launch_detail = _launch_recovery_once(root, log)
            else:
                _append_log_best_effort(
                    log,
                    f"\n[FAIL-CLOSED] {state}: runtime guard liberado después de persistir recuperación; Nova no fue relanzada.\n",
                )

        if no_launch:
            return rc
        if ok:
            return 0 if launched else 3
        return rc or 2
    finally:
        _release_supervisor_mutex_best_effort(supervisor_mutex, log)


if __name__ == "__main__":
    raise SystemExit(main())
