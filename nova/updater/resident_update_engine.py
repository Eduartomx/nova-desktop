from __future__ import annotations

"""Hardened resident update entry used only after update_runner coordination.

It reuses Nova's existing GitHub download/staging/backup implementation while
replacing dependency execution and transaction recovery with the v0.9.9
Job-Object/quarantine contract.
"""

import argparse
from pathlib import Path
import subprocess
import sys

try:
    from . import nova_updater as base
    from .pip_safety import (
        PipContainmentSetupError,
        PipTerminationResult,
        launch_pip_process,
        terminate_pip_tree,
        verify_normal_completion,
    )
    from .recovery_bootstrap import create_quarantine_journal, create_rollback_recovery_journal
    from .recovery_state import RecoveryJournalError, load_journal
except ImportError:  # direct script execution
    import nova_updater as base
    from pip_safety import (
        PipContainmentSetupError,
        PipTerminationResult,
        launch_pip_process,
        terminate_pip_tree,
        verify_normal_completion,
    )
    from recovery_bootstrap import create_quarantine_journal, create_rollback_recovery_journal
    from recovery_state import RecoveryJournalError, load_journal

PIP_TERMINATION_UNCONFIRMED_EXIT_CODE = 6
RECOVERY_REQUIRED_EXIT_CODE = 7


class DependencyInstallError(RuntimeError):
    def __init__(self, message: str, *, dependency_started: bool, termination: PipTerminationResult | None = None):
        super().__init__(message)
        self.dependency_started = bool(dependency_started)
        self.termination = termination


class PipTerminationUnconfirmedError(DependencyInstallError):
    def __init__(self, message: str, result: PipTerminationResult):
        super().__init__(message, dependency_started=True, termination=result)
        self.result = result


class RecoveryRequiredError(RuntimeError):
    pass


def _ensure_no_recovery_pending() -> None:
    """Second fail-closed gate before GitHub access, staging or file writes."""
    try:
        journal = load_journal(base.ROOT)
    except RecoveryJournalError as exc:
        raise RecoveryRequiredError(
            f"journal de recuperación no verificable: {type(exc).__name__}"
        ) from exc
    if journal is not None and bool(journal.get("recovery_required")) and journal.get("state") != "cleared":
        raise RecoveryRequiredError(
            f"recuperación persistente activa: {journal.get('state') or 'unknown'}"
        )


def _install_requirements(timeout_seconds=None) -> bool:
    req = base.ROOT / "requirements.txt"
    if not req.exists():
        return False
    timeout = base._normalize_pip_timeout(timeout_seconds)
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    print(f"Actualizando dependencias Python (timeout {timeout:g}s; contención segura)...")
    try:
        proc = launch_pip_process(cmd, cwd=str(base.ROOT))
    except PipContainmentSetupError as exc:
        raise DependencyInstallError(
            f"No pude establecer contención autoritativa para pip antes de ejecutarlo: {exc}",
            dependency_started=False,
        ) from exc

    try:
        return_code = int(proc.wait(timeout=timeout))
    except subprocess.TimeoutExpired as exc:
        result = terminate_pip_tree(proc, base.PIP_TERMINATE_GRACE_SECONDS)
        if result.terminated_confirmed and result.rollback_allowed:
            raise DependencyInstallError(
                f"pip excedió el timeout de {timeout:g}s; la terminación del contenedor fue confirmada y el rollback puede ejecutarse.",
                dependency_started=True,
                termination=result,
            ) from exc
        raise PipTerminationUnconfirmedError(
            f"pip excedió el timeout de {timeout:g}s y su terminación completa no pudo demostrarse; se activa cuarentena persistente.",
            result,
        ) from exc
    except Exception as exc:
        result = terminate_pip_tree(proc, base.PIP_TERMINATE_GRACE_SECONDS)
        if result.terminated_confirmed and result.rollback_allowed:
            raise DependencyInstallError(
                f"falló la espera de pip ({type(exc).__name__}); contenedor terminado de forma verificable.",
                dependency_started=True,
                termination=result,
            ) from exc
        raise PipTerminationUnconfirmedError(
            f"falló la espera de pip ({type(exc).__name__}) y la terminación no pudo confirmarse; se activa cuarentena persistente.",
            result,
        ) from exc

    completion = verify_normal_completion(proc, base.PIP_TERMINATE_GRACE_SECONDS)
    if completion is not None:
        if not completion.terminated_confirmed or not completion.rollback_allowed:
            raise PipTerminationUnconfirmedError(
                "pip terminó su proceso raíz, pero el Job Object no pudo confirmar que no quedaran descendientes activos.",
                completion,
            )
        if not str(completion.detail).startswith("normal Job Object completion confirmed"):
            raise DependencyInstallError(
                "pip dejó descendientes después de finalizar y fue necesario terminar el Job Object; se requiere rollback.",
                dependency_started=True,
                termination=completion,
            )
    if return_code != 0:
        raise DependencyInstallError("pip install -r requirements.txt falló", dependency_started=True, termination=completion)
    return True


def _write_quarantine_compat_status(backup: Path, journal: dict) -> None:
    payload = {
        "ok": False,
        "status": "pip_termination_unconfirmed",
        "state": "pip_termination_unconfirmed",
        "files_rollback_ok": False,
        "files_rollback_attempted": False,
        "dependencies_may_have_changed": True,
        "recovery_required": True,
        "remaining_processes": list(journal.get("remaining_processes") or []),
        "errors": list(journal.get("errors") or []),
        "backup_path": str(journal.get("backup_path") or ""),
        "attempt_id": str(journal.get("attempt_id") or ""),
        "generation": int(journal.get("generation") or 1),
        "message": "Terminación de pip no confirmada; cuarentena persistente activa.",
        "timestamp": str(journal.get("updated_at") or ""),
    }
    try:
        base._atomic_write_json(Path(backup) / "rollback_status.json", payload)
    except Exception:
        pass


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
):
    _ensure_no_recovery_pending()
    manifest = base.build_transaction(stage, new_files, previous)
    touched = manifest["modified_existing"] + manifest["deleted_existing"] + manifest["created_new"]
    if not touched:
        base.write_managed(new_files, tag)
        return None, manifest

    backup = base.create_backup(manifest, old_version, new_version, backup_root=backup_root)
    print(f"Backup: {backup}")
    dependencies_started = False
    requirements_changed = "requirements.txt" in set(manifest["modified_existing"] + manifest["created_new"])
    try:
        base.apply_transaction(stage, manifest)
        if requirements_changed:
            try:
                dependencies_started = bool(_install_requirements(timeout_seconds=pip_timeout_seconds))
            except DependencyInstallError as exc:
                dependencies_started = bool(exc.dependency_started)
                raise
        ok, detail = base.validate_install()
        if not ok:
            raise RuntimeError(detail)
        print(detail)
        base.write_managed(new_files, tag)
        return backup, manifest
    except PipTerminationUnconfirmedError as update_error:
        result = update_error.result
        print("La terminación de pip no pudo confirmarse; se conserva el backup y se activa cuarentena persistente.")
        journal = create_quarantine_journal(
            base.ROOT,
            backup,
            backup_root=Path(backup_root) if backup_root is not None else None,
            remaining_processes=list(result.remaining_processes or []),
            errors=list(result.termination_errors or []),
            recovery_detail=str(update_error),
            identity_verification_required=not bool(result.identity_inspection_complete),
        )
        _write_quarantine_compat_status(backup, journal)
        raise
    except Exception as update_error:
        if isinstance(update_error, DependencyInstallError):
            dependencies_started = bool(update_error.dependency_started)
        print("La actualización falló; restaurando transacción cuando es seguro...")
        rollback_ok = False
        rollback_error = None
        try:
            base.restore_backup(
                backup,
                dependencies_may_have_changed=False,
                recovery_detail=str(update_error),
            )
            rollback_ok = True
        except Exception as exc:
            rollback_error = exc

        if dependencies_started or not rollback_ok:
            create_rollback_recovery_journal(
                base.ROOT,
                backup,
                backup_root=Path(backup_root) if backup_root is not None else None,
                rollback_ok=rollback_ok,
                dependencies_may_have_changed=dependencies_started,
                errors=[] if rollback_error is None else [f"rollback_failed:{type(rollback_error).__name__}"],
                recovery_detail=str(update_error),
            )
            if rollback_error is not None:
                raise RecoveryRequiredError(
                    f"actualización falló y el rollback quedó incompleto; backup preservado: {type(rollback_error).__name__}"
                ) from update_error
            raise RecoveryRequiredError(
                "archivos restaurados, pero las dependencias pudieron cambiar; recuperación/validación persistente requerida"
            ) from update_error

        raise update_error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--yes", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    if not args.yes:
        print("[ERROR] resident_update_engine es un camino interno del supervisor.")
        return 4
    try:
        _ensure_no_recovery_pending()
        base.execute_transaction = execute_transaction
        cfg = base.load_config()
        current = base.version_text()
        release = base.get_release(cfg)
        latest = str(release.get("tag_name", "")).lstrip("vV")
        notes = (release.get("body") or "").strip()
        print("=" * 58)
        print("NOVA UPDATER 2.3 - Resident recovery hardened")
        print("=" * 58)
        print(f"Repositorio : {cfg['repository']}")
        print(f"Canal       : {cfg.get('channel', 'stable')}")
        print(f"Instalada   : {current}")
        print(f"Disponible  : {latest}")
        if base.version_key(latest) <= base.version_key(current):
            print("\nNova ya está actualizada.")
            return 0
        if notes:
            print("\nCambios:\n" + notes[:2500])
        base.sync_release(cfg, release)
        print("\n" + "=" * 58)
        print(f"NOVA {latest} INSTALADA DESDE GITHUB")
        print("=" * 58)
        return 0
    except PipTerminationUnconfirmedError as exc:
        print(f"[ERROR] {exc}")
        return PIP_TERMINATION_UNCONFIRMED_EXIT_CODE
    except RecoveryRequiredError as exc:
        print(f"[RECOVERY REQUIRED] {exc}")
        return RECOVERY_REQUIRED_EXIT_CODE
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
