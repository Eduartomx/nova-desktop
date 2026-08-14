from __future__ import annotations

"""Resident update transaction engine.

This module is import-only in production.  ``update_runner`` invokes
``run_supervised_update`` in the same process while it owns the supervisor
mutex and runtime guard.  Direct CLI execution is deliberately inert.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

try:
    from . import nova_updater as base
    from .pip_safety import (
        PipContainmentSetupError,
        PipTerminationResult,
        PsutilProcessTree,
        launch_pip_process,
        terminate_pip_tree,
        verify_normal_completion,
    )
    from .recovery_state import (
        RecoveryJournalError,
        append_journal_error,
        capture_dependency_snapshot,
        create_transaction_journal,
        load_journal,
        prepare_stable_recovery_runtime,
        restore_backup_idempotent,
        transition_journal,
        validate_backup_path,
        validate_restored_install,
    )
except ImportError:
    import nova_updater as base
    from pip_safety import (
        PipContainmentSetupError,
        PipTerminationResult,
        PsutilProcessTree,
        launch_pip_process,
        terminate_pip_tree,
        verify_normal_completion,
    )
    from recovery_state import (
        RecoveryJournalError,
        append_journal_error,
        capture_dependency_snapshot,
        create_transaction_journal,
        load_journal,
        prepare_stable_recovery_runtime,
        restore_backup_idempotent,
        transition_journal,
        validate_backup_path,
        validate_restored_install,
    )

PIP_TERMINATION_UNCONFIRMED_EXIT_CODE = 6
RECOVERY_REQUIRED_EXIT_CODE = 7
DIRECT_EXECUTION_BLOCKED_EXIT_CODE = 4

CrashHook = Callable[[str, dict[str, Any]], None]


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


def _hook(callback: CrashHook | None, point: str, **context: Any) -> None:
    if callback is not None:
        callback(point, dict(context))


@contextmanager
def _base_root(root: Path):
    """Temporarily point the established updater helpers at ``root``."""
    root = Path(root).resolve()
    previous = {
        "ROOT": getattr(base, "ROOT", None),
        "CONFIG_PATH": getattr(base, "CONFIG_PATH", None),
        "MANAGED_PATH": getattr(base, "MANAGED_PATH", None),
    }
    base.ROOT = root
    base.CONFIG_PATH = root / "updater" / "update_config.json"
    base.MANAGED_PATH = root / "updater" / "managed_files.json"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is not None:
                setattr(base, name, value)


def _ensure_no_recovery_pending(root: Path) -> None:
    try:
        journal = load_journal(root)
    except RecoveryJournalError as exc:
        raise RecoveryRequiredError(f"journal de recuperación no verificable: {type(exc).__name__}") from exc
    if journal is not None and bool(journal.get("recovery_required")) and journal.get("state") != "cleared":
        raise RecoveryRequiredError(f"recuperación persistente activa: {journal.get('state') or 'unknown'}")


def _process_identity(proc) -> tuple[list[dict[str, Any]], bool]:
    pid = int(getattr(proc, "pid", 0) or 0)
    if pid <= 0:
        return [], False
    if os.name == "nt" and bool(getattr(proc, "authoritative_containment", False)):
        try:
            creation = proc.api.process_creation_time(pid)
            if creation:
                return [{"pid": pid, "creation_time": int(creation), "role": "pip_root_or_descendant"}], True
        except Exception:
            return [], True
        return [], True
    try:
        identity = PsutilProcessTree().identity(pid)
        return ([identity] if identity else []), bool(identity)
    except Exception:
        return [], False


def _install_requirements(timeout_seconds=None, *, on_started=None) -> bool:
    req = Path(base.ROOT) / "requirements.txt"
    if not req.exists():
        return False
    timeout = base._normalize_pip_timeout(timeout_seconds)
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    print(f"Actualizando dependencias Python (timeout {timeout:g}s; contención segura)...")
    try:
        proc = launch_pip_process(cmd, cwd=str(base.ROOT))
    except PipContainmentSetupError as exc:
        raise DependencyInstallError(
            f"No pude establecer contención autoritativa para pip antes de ejecutarlo: {type(exc).__name__}",
            dependency_started=False,
        ) from exc

    if on_started is not None:
        try:
            on_started(proc)
        except BaseException as callback_error:
            result = terminate_pip_tree(proc, base.PIP_TERMINATE_GRACE_SECONDS)
            if result.terminated_confirmed and result.rollback_allowed:
                raise DependencyInstallError(
                    f"falló la publicación del estado de pip ({type(callback_error).__name__}); contenedor terminado de forma verificable",
                    dependency_started=True,
                    termination=result,
                ) from callback_error
            raise PipTerminationUnconfirmedError(
                f"falló la publicación del estado de pip ({type(callback_error).__name__}) y no pudo confirmarse la terminación",
                result,
            ) from callback_error

    try:
        return_code = int(proc.wait(timeout=timeout))
    except subprocess.TimeoutExpired as exc:
        result = terminate_pip_tree(proc, base.PIP_TERMINATE_GRACE_SECONDS)
        if result.terminated_confirmed and result.rollback_allowed:
            raise DependencyInstallError(
                f"pip excedió el timeout de {timeout:g}s; la terminación del contenedor fue confirmada",
                dependency_started=True,
                termination=result,
            ) from exc
        raise PipTerminationUnconfirmedError(
            f"pip excedió el timeout de {timeout:g}s y su terminación completa no pudo demostrarse",
            result,
        ) from exc
    except BaseException as exc:
        result = terminate_pip_tree(proc, base.PIP_TERMINATE_GRACE_SECONDS)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if result.terminated_confirmed and result.rollback_allowed:
            raise DependencyInstallError(
                f"falló la espera de pip ({type(exc).__name__}); contenedor terminado de forma verificable",
                dependency_started=True,
                termination=result,
            ) from exc
        raise PipTerminationUnconfirmedError(
            f"falló la espera de pip ({type(exc).__name__}) y la terminación no pudo confirmarse",
            result,
        ) from exc

    completion = verify_normal_completion(proc, base.PIP_TERMINATE_GRACE_SECONDS)
    if completion is not None:
        if not completion.terminated_confirmed or not completion.rollback_allowed:
            raise PipTerminationUnconfirmedError(
                "pip terminó su proceso raíz, pero el contenedor no pudo confirmar una terminación completa",
                completion,
            )
        if not str(completion.detail).startswith("normal Job Object completion confirmed"):
            raise DependencyInstallError(
                "pip dejó descendientes después de finalizar y fue necesario cerrar el contenedor",
                dependency_started=True,
                termination=completion,
            )
    if return_code != 0:
        raise DependencyInstallError("pip install -r requirements.txt falló", dependency_started=True, termination=completion)
    return True
