from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import subprocess
from typing import Any


@dataclass(frozen=True)
class PipTerminationResult:
    terminated_confirmed: bool
    direct_process_terminated: bool
    remaining_pids: list[int] = field(default_factory=list)
    termination_errors: list[str] = field(default_factory=list)
    detail: str = ""


class PsutilProcessTree:
    """Best-effort containment/inspection for a process tree owned by Nova."""

    def __init__(self):
        import psutil
        self.psutil = psutil

    def snapshot(self, root_pid: int) -> set[int]:
        root_pid = int(root_pid)
        pids: set[int] = set()
        try:
            root = self.psutil.Process(root_pid)
            pids.add(root_pid)
            for child in root.children(recursive=True):
                pids.add(int(child.pid))
        except self.psutil.NoSuchProcess:
            pass

        if os.name != "nt":
            # Pip is started in a fresh session, so PGID == root PID.  Scanning
            # the group also catches children that were re-parented after the
            # direct pip process exited.
            try:
                for row in self.psutil.process_iter(["pid"]):
                    pid = int(row.info.get("pid") or 0)
                    if pid <= 0:
                        continue
                    try:
                        if os.getpgid(pid) == root_pid:
                            pids.add(pid)
                    except (ProcessLookupError, PermissionError, OSError):
                        continue
            except Exception:
                # The caller treats snapshot errors as inability to confirm.
                raise
        return pids

    def _act(self, root_pid: int, pids: set[int], *, force: bool) -> None:
        if os.name != "nt":
            sig = signal.SIGKILL if force else signal.SIGTERM
            try:
                os.killpg(int(root_pid), sig)
            except ProcessLookupError:
                pass
            return

        # On Windows psutil recursive ownership is used.  Children are acted on
        # before the direct pip process to reduce the window for new descendants.
        ordered = sorted({int(pid) for pid in pids if int(pid) > 0 and int(pid) != int(root_pid)})
        ordered.append(int(root_pid))
        for pid in ordered:
            try:
                process = self.psutil.Process(pid)
                process.kill() if force else process.terminate()
            except self.psutil.NoSuchProcess:
                continue

    def terminate(self, root_pid: int, pids: set[int]) -> None:
        self._act(root_pid, pids, force=False)

    def kill(self, root_pid: int, pids: set[int]) -> None:
        self._act(root_pid, pids, force=True)

    def alive(self, pids: set[int]) -> set[int]:
        alive: set[int] = set()
        for pid in {int(value) for value in pids if int(value) > 0}:
            try:
                process = self.psutil.Process(pid)
                if process.is_running() and process.status() != self.psutil.STATUS_ZOMBIE:
                    alive.add(pid)
            except self.psutil.NoSuchProcess:
                continue
        return alive


def pip_popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _record(errors: list[str], label: str, exc: BaseException) -> None:
    errors.append(f"{label}:{type(exc).__name__}:{str(exc)[:240]}")


def _snapshot(api, root_pid: int, known: set[int], errors: list[str], label: str) -> bool:
    try:
        known.update(int(pid) for pid in api.snapshot(root_pid) if int(pid) > 0)
        return True
    except Exception as exc:
        _record(errors, label, exc)
        return False


def _wait_direct(proc, timeout: float, errors: list[str], label: str) -> bool:
    try:
        proc.wait(timeout=max(0.0, float(timeout)))
        return True
    except subprocess.TimeoutExpired:
        errors.append(f"{label}:TimeoutExpired")
        return False
    except Exception as exc:
        _record(errors, label, exc)
        return False


def _direct_is_terminated(proc) -> bool:
    try:
        return proc.poll() is not None
    except Exception:
        return False


def terminate_pip_tree(proc, grace_seconds: float, *, tree_api=None) -> PipTerminationResult:
    """Terminate and verify pip plus descendants without assuming kill == exit."""
    api = tree_api or PsutilProcessTree()
    root_pid = int(getattr(proc, "pid", 0) or 0)
    errors: list[str] = []
    known: set[int] = {root_pid} if root_pid > 0 else set()
    inspection_ok = root_pid > 0

    if root_pid <= 0:
        errors.append("root_pid:unavailable")
    else:
        inspection_ok = _snapshot(api, root_pid, known, errors, "snapshot_initial") and inspection_ok

    try:
        proc.terminate()
    except Exception as exc:
        _record(errors, "terminate_direct", exc)
    if root_pid > 0:
        try:
            api.terminate(root_pid, set(known))
        except Exception as exc:
            _record(errors, "terminate_tree", exc)

    direct_terminated = _wait_direct(proc, grace_seconds, errors, "wait_after_terminate")
    if root_pid > 0:
        inspection_ok = _snapshot(api, root_pid, known, errors, "snapshot_after_terminate") and inspection_ok
    try:
        remaining = set(api.alive(set(known))) if root_pid > 0 else set()
    except Exception as exc:
        _record(errors, "alive_after_terminate", exc)
        inspection_ok = False
        remaining = set(known)

    if remaining or not direct_terminated:
        try:
            proc.kill()
        except Exception as exc:
            _record(errors, "kill_direct", exc)
        if root_pid > 0:
            try:
                # Re-snapshot immediately before force to close the common race
                # where pip creates another child during graceful termination.
                inspection_ok = _snapshot(api, root_pid, known, errors, "snapshot_before_kill") and inspection_ok
                api.kill(root_pid, set(known))
            except Exception as exc:
                _record(errors, "kill_tree", exc)
        direct_terminated = _wait_direct(proc, grace_seconds, errors, "wait_after_kill") or _direct_is_terminated(proc)

    if root_pid > 0:
        inspection_ok = _snapshot(api, root_pid, known, errors, "snapshot_final") and inspection_ok
    try:
        remaining = set(api.alive(set(known))) if root_pid > 0 else set()
    except Exception as exc:
        _record(errors, "alive_final", exc)
        inspection_ok = False
        remaining = set(known)

    direct_terminated = bool(direct_terminated or _direct_is_terminated(proc))
    if not direct_terminated and root_pid > 0:
        remaining.add(root_pid)
    elif direct_terminated:
        remaining.discard(root_pid)

    confirmed = bool(inspection_ok and direct_terminated and not remaining)
    remaining_list = sorted(int(pid) for pid in remaining if int(pid) > 0)[:64]
    if confirmed:
        detail = "terminación de pip y descendientes confirmada"
    else:
        detail = "terminación de pip no confirmada"
        if remaining_list:
            detail += "; PID restantes: " + ", ".join(str(pid) for pid in remaining_list)
        if not inspection_ok:
            detail += "; inspección del árbol incompleta"
    return PipTerminationResult(
        terminated_confirmed=confirmed,
        direct_process_terminated=direct_terminated,
        remaining_pids=remaining_list,
        termination_errors=errors[:64],
        detail=detail,
    )
