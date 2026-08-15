from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .process_launch import (
        detached_hidden_creation_flags,
        select_console_python,
        select_gui_python,
    )
except ImportError:
    from process_launch import (
        detached_hidden_creation_flags,
        select_console_python,
        select_gui_python,
    )

SUPERVISOR_ALREADY_RUNNING_CODE = 5
PIP_TERMINATION_UNCONFIRMED_CODE = 6


def nova_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_version(root: Path) -> str:
    path = root / "NOVA_VERSION.txt"
    if not path.exists():
        return "0.0.0"
    return path.read_text(encoding="utf-8", errors="ignore").strip().lstrip("vV") or "0.0.0"


def console_python(root: Path) -> Path:
    """Compatibility facade for callers of the legacy runner API."""
    return select_console_python(root)


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


class ProcessCapture:
    """Stable reference to a process captured before shutdown coordination."""

    def __init__(self, pid: int, handle=None, *, already_terminated=False, creation_time: int = 0):
        self.pid = int(pid)
        self.handle = handle
        self.already_terminated = bool(already_terminated)
        self.creation_time = int(creation_time or 0)

    @classmethod
    def open(cls, pid: int):
        pid = int(pid)
        if pid <= 0:
            raise ValueError("invalid pid")
        if os.name != "nt":
            try:
                import psutil
                process = psutil.Process(pid)
                creation = int(float(process.create_time()) * 1_000_000)
                if not process.is_running():
                    return cls(pid, already_terminated=True, creation_time=creation)
                return cls(pid, creation_time=creation)
            except Exception as exc:
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, OSError):
                    return cls(pid, already_terminated=True)
                raise exc

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return cls(pid, already_terminated=True)
            raise ctypes.WinError(error)
        created = _FILETIME()
        exited = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        capture = cls(pid, handle, creation_time=_filetime_value(created))
        capture._kernel32 = kernel32
        return capture

    def matches_creation_time(self, expected: int) -> bool:
        return int(expected or 0) > 0 and self.creation_time > 0 and self.creation_time == int(expected)

    def wait(self, timeout: float) -> bool:
        if self.already_terminated:
            return True
        timeout = max(0.0, float(timeout))
        if os.name == "nt" and self.handle:
            WAIT_OBJECT_0 = 0
            WAIT_TIMEOUT = 258
            result = int(self._kernel32.WaitForSingleObject(self.handle, min(int(timeout * 1000), 0xFFFFFFFE)))
            if result == WAIT_OBJECT_0:
                return True
            if result == WAIT_TIMEOUT:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        event = threading.Event()
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except (ProcessLookupError, OSError):
                return True
            event.wait(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            os.kill(self.pid, 0)
            return False
        except (ProcessLookupError, OSError):
            return True

    def close(self) -> None:
        if os.name == "nt" and self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def wait_for_parent(pid: int | None, timeout: float = 20.0) -> bool:
    if not pid or pid <= 0:
        return True
    capture = ProcessCapture.open(int(pid))
    try:
        return capture.wait(timeout)
    finally:
        capture.close()


@dataclass
class ShutdownCoordination:
    ok: bool
    error: str = ""
    owner_pid: int = 0
    owner_id: str = ""
    owner_role: str = "runtime"
    owner_process_creation_time: int = 0
    command_sent: bool = False
    process_terminated: bool = False
    lock_acquired: bool = False
    guard: Any = field(default=None, repr=False, compare=False)

    def release_guard(self) -> None:
        guard = self.guard
        self.guard = None
        if guard is not None:
            guard.release()


def _runtime_components(root: Path, *, lock_factory=None, guard_factory=None, mailbox=None):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from assistant.instance_lock import InstanceLock, runtime_paths
    from assistant.instance_commands import InstanceCommandMailbox

    paths = runtime_paths()
    if lock_factory is None:
        lock_factory = lambda: InstanceLock(path=paths.lock, owner_path=paths.owner, publish_owner=False, role="observer")
    if guard_factory is None:
        if lock_factory is not None and getattr(lock_factory, "_nova_injected", False):
            guard_factory = lock_factory
        else:
            guard_factory = lambda: InstanceLock(path=paths.lock, owner_path=paths.owner, publish_owner=False, role="updater")
    if mailbox is None:
        mailbox = InstanceCommandMailbox(paths.commands)
    return lock_factory, guard_factory, mailbox


def _acquire_supervisor_mutex(root: Path, supervisor_lock_factory=None):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if supervisor_lock_factory is None:
        from assistant.update_supervisor import create_supervisor_mutex
        supervisor_lock_factory = create_supervisor_mutex
    lock = supervisor_lock_factory()
    if not lock.acquire():
        return None
    return lock


def _try_guard(guard_factory):
    guard = guard_factory()
    try:
        if guard.acquire():
            return guard
        return None
    except Exception:
        try:
            guard.release()
        except Exception:
            pass
        raise


def _owner_values(owner: dict | None) -> tuple[int, str, str, int]:
    owner = owner or {}
    return (
        int(owner.get("pid") or 0),
        str(owner.get("owner_id") or ""),
        str(owner.get("role") or "runtime"),
        int(owner.get("process_creation_time") or 0),
    )


def _owner_values_best_effort(owner: dict | None) -> tuple[int, str, str, int]:
    try:
        return _owner_values(owner)
    except Exception:
        return 0, "", "runtime", 0


def _owner_unchanged(observer, *, pid: int, owner_id: str, creation_time: int, role: str) -> bool:
    verify = observer.read_owner()
    v_pid, v_owner_id, v_role, v_creation = _owner_values(verify)
    return v_pid == pid and v_owner_id == owner_id and v_role == role and v_creation == creation_time


def _publish_updater_guard(guard) -> tuple[bool, str]:
    publish = getattr(guard, "publish_owner_metadata", None)
    if not callable(publish):
        return True, ""
    try:
        publish()
        return True, ""
    except Exception as exc:
        return False, f"updater_guard_metadata_failed:{type(exc).__name__}"


def _release_failed_guard(guard) -> tuple[Any, str]:
    try:
        guard.release()
        return None, ""
    except Exception as exc:
        return guard, f":guard_release_failed:{type(exc).__name__}"


def _guarded_previous_owner_result(
    guard,
    owner_snapshot: dict | None,
    *,
    expected_pid: int,
    process_factory,
    deadline: float,
) -> ShutdownCoordination:
    owner_pid, owner_id, owner_role, owner_creation = _owner_values(owner_snapshot)
    target_pid = int(expected_pid or owner_pid or 0)
    target_creation = owner_creation if target_pid > 0 and target_pid == owner_pid else 0
    process_terminated = target_pid <= 0

    if target_pid > 0:
        try:
            captured = process_factory(target_pid)
        except Exception as exc:
            retained, release_error = _release_failed_guard(guard)
            return ShutdownCoordination(
                False,
                f"owner_process_capture_failed:{type(exc).__name__}{release_error}",
                owner_pid=target_pid,
                owner_id=owner_id,
                owner_role=owner_role,
                owner_process_creation_time=target_creation,
                lock_acquired=True,
                guard=retained,
            )
        try:
            if getattr(captured, "already_terminated", False):
                process_terminated = True
            elif target_creation <= 0:
                retained, release_error = _release_failed_guard(guard)
                return ShutdownCoordination(
                    False,
                    "runtime_owner_identity_unavailable" + release_error,
                    owner_pid=target_pid,
                    owner_id=owner_id,
                    owner_role=owner_role,
                    owner_process_creation_time=0,
                    process_terminated=False,
                    lock_acquired=True,
                    guard=retained,
                )
            elif not captured.matches_creation_time(target_creation):
                process_terminated = True
            else:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    process_terminated = bool(captured.wait(remaining))
                except Exception as exc:
                    retained, release_error = _release_failed_guard(guard)
                    return ShutdownCoordination(
                        False,
                        f"owner_process_wait_failed:{type(exc).__name__}{release_error}",
                        owner_pid=target_pid,
                        owner_id=owner_id,
                        owner_role=owner_role,
                        owner_process_creation_time=target_creation,
                        lock_acquired=True,
                        guard=retained,
                    )
                if not process_terminated:
                    retained, release_error = _release_failed_guard(guard)
                    return ShutdownCoordination(
                        False,
                        "owner_process_timeout" + release_error,
                        owner_pid=target_pid,
                        owner_id=owner_id,
                        owner_role=owner_role,
                        owner_process_creation_time=target_creation,
                        lock_acquired=True,
                        guard=retained,
                    )
        finally:
            try:
                captured.close()
            except Exception:
                pass

    published, publish_error = _publish_updater_guard(guard)
    if not published:
        retained, release_error = _release_failed_guard(guard)
        return ShutdownCoordination(
            False,
            publish_error + release_error,
            owner_pid=target_pid or owner_pid,
            owner_id=owner_id,
            owner_role=owner_role if owner_snapshot else "none",
            owner_process_creation_time=target_creation,
            process_terminated=process_terminated,
            lock_acquired=True,
            guard=retained,
        )
    return ShutdownCoordination(
        True,
        owner_pid=target_pid or owner_pid,
        owner_id=owner_id,
        owner_role=owner_role if owner_snapshot else "none",
        owner_process_creation_time=target_creation,
        process_terminated=True,
        lock_acquired=True,
        guard=guard,
    )


def _acquire_verified_guard(observer, guard_factory, deadline: float, process_factory):
    """Acquire updater guard after the captured runtime already terminated."""
    event = threading.Event()
    last = None
    while time.monotonic() <= deadline:
        guard = None
        try:
            snapshot = observer.read_owner()
            guard = _try_guard(guard_factory)
        except Exception as exc:
            return ShutdownCoordination(
                False,
                f"runtime_guard_acquire_failed:{type(exc).__name__}",
                process_terminated=True,
                lock_acquired=guard is not None,
                guard=guard,
            )
        if guard is not None:
            try:
                result = _guarded_previous_owner_result(
                    guard,
                    snapshot,
                    expected_pid=0,
                    process_factory=process_factory,
                    deadline=deadline,
                )
            except Exception as exc:
                return ShutdownCoordination(
                    False,
                    f"runtime_guard_validation_failed:{type(exc).__name__}",
                    process_terminated=True,
                    lock_acquired=True,
                    guard=guard,
                )
            if not result.ok:
                result.process_terminated = True
            return result
        last = snapshot
        event.wait(min(0.05, max(0.0, deadline - time.monotonic())))
    pid, owner_id, role, creation = _owner_values_best_effort(last)
    return ShutdownCoordination(
        False,
        "runtime_lock_timeout_after_process_exit",
        owner_pid=pid,
        owner_id=owner_id,
        owner_role=role,
        owner_process_creation_time=creation,
        process_terminated=True,
    )


def _coordinate_runtime_shutdown_impl(
    root: Path,
    timeout: float = 20.0,
    *,
    expected_pid: int = 0,
    lock_factory=None,
    guard_factory=None,
    mailbox=None,
    process_factory=None,
) -> ShutdownCoordination:
    injected_lock_factory = lock_factory
    if injected_lock_factory is not None and guard_factory is None:
        guard_factory = injected_lock_factory
    observer_factory, guard_factory, mailbox = _runtime_components(
        root,
        lock_factory=lock_factory,
        guard_factory=guard_factory,
        mailbox=mailbox,
    )
    process_factory = process_factory or ProcessCapture.open
    deadline = time.monotonic() + max(0.0, float(timeout))
    observer = observer_factory()
    owner_snapshot = observer.read_owner()

    try:
        guard = _try_guard(guard_factory)
    except Exception as exc:
        owner_pid, owner_id, owner_role, owner_creation = _owner_values_best_effort(owner_snapshot)
        return ShutdownCoordination(
            False,
            f"runtime_guard_open_failed:{type(exc).__name__}",
            owner_pid=owner_pid,
            owner_id=owner_id,
            owner_role=owner_role,
            owner_process_creation_time=owner_creation,
        )
    if guard is not None:
        try:
            stable_snapshot = observer.read_owner() or owner_snapshot
            return _guarded_previous_owner_result(
                guard,
                stable_snapshot,
                expected_pid=int(expected_pid or 0),
                process_factory=process_factory,
                deadline=deadline,
            )
        except Exception as exc:
            owner_pid, owner_id, owner_role, owner_creation = _owner_values_best_effort(owner_snapshot)
            return ShutdownCoordination(
                False,
                f"runtime_guard_validation_failed:{type(exc).__name__}",
                owner_pid=owner_pid,
                owner_id=owner_id,
                owner_role=owner_role,
                owner_process_creation_time=owner_creation,
                lock_acquired=True,
                guard=guard,
            )

    event = threading.Event()
    last_identity_error = "runtime_owner_metadata_unavailable"
    owner_pid = 0
    owner_id = ""
    owner_role = "runtime"
    owner_creation = 0
    while time.monotonic() <= deadline:
        owner = observer.read_owner()
        owner_pid, owner_id, owner_role, owner_creation = _owner_values(owner)
        if not owner or owner_pid <= 0 or not owner_id:
            last_identity_error = "runtime_owner_metadata_unavailable"
        elif owner_creation <= 0:
            last_identity_error = "runtime_owner_identity_unavailable"
        elif owner_role not in {"runtime", "updater"}:
            last_identity_error = "runtime_owner_role_invalid"
        elif expected_pid and owner_pid != int(expected_pid):
            return ShutdownCoordination(
                False,
                "runtime_owner_pid_mismatch",
                owner_pid=owner_pid,
                owner_id=owner_id,
                owner_role=owner_role,
                owner_process_creation_time=owner_creation,
            )
        else:
            try:
                captured = process_factory(owner_pid)
            except Exception as exc:
                return ShutdownCoordination(
                    False,
                    f"owner_process_capture_failed:{type(exc).__name__}",
                    owner_pid=owner_pid,
                    owner_id=owner_id,
                    owner_role=owner_role,
                    owner_process_creation_time=owner_creation,
                )
            try:
                if getattr(captured, "already_terminated", False):
                    last_identity_error = "runtime_owner_stale_while_locked"
                elif not captured.matches_creation_time(owner_creation):
                    last_identity_error = "runtime_owner_identity_mismatch"
                elif not _owner_unchanged(observer, pid=owner_pid, owner_id=owner_id, creation_time=owner_creation, role=owner_role):
                    last_identity_error = "runtime_owner_changed_during_capture"
                else:
                    command_sent = False
                    if owner_role == "runtime":
                        try:
                            command_sent = bool(mailbox.send("shutdown_for_update", target_owner_id=owner_id))
                        except Exception as exc:
                            return ShutdownCoordination(
                                False,
                                f"shutdown_command_delivery_exception:{type(exc).__name__}",
                                owner_pid=owner_pid,
                                owner_id=owner_id,
                                owner_role=owner_role,
                                owner_process_creation_time=owner_creation,
                            )
                        if not command_sent:
                            return ShutdownCoordination(
                                False,
                                "shutdown_command_delivery_failed",
                                owner_pid=owner_pid,
                                owner_id=owner_id,
                                owner_role=owner_role,
                                owner_process_creation_time=owner_creation,
                            )
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        process_terminated = bool(captured.wait(remaining))
                    except Exception as exc:
                        return ShutdownCoordination(
                            False,
                            f"owner_process_wait_failed:{type(exc).__name__}",
                            owner_pid=owner_pid,
                            owner_id=owner_id,
                            owner_role=owner_role,
                            owner_process_creation_time=owner_creation,
                            command_sent=command_sent,
                        )
                    if not process_terminated:
                        return ShutdownCoordination(
                            False,
                            "owner_process_timeout",
                            owner_pid=owner_pid,
                            owner_id=owner_id,
                            owner_role=owner_role,
                            owner_process_creation_time=owner_creation,
                            command_sent=command_sent,
                        )
                    guarded = _acquire_verified_guard(observer, guard_factory, deadline, process_factory)
                    if not guarded.ok:
                        guarded.command_sent = command_sent
                        if not guarded.owner_pid:
                            guarded.owner_pid = owner_pid
                            guarded.owner_id = owner_id
                            guarded.owner_role = owner_role
                            guarded.owner_process_creation_time = owner_creation
                        guarded.process_terminated = True
                        return guarded
                    guarded.command_sent = command_sent
                    guarded.owner_pid = owner_pid
                    guarded.owner_id = owner_id
                    guarded.owner_role = owner_role
                    guarded.owner_process_creation_time = owner_creation
                    guarded.process_terminated = True
                    return guarded
            finally:
                try:
                    captured.close()
                except Exception:
                    pass

        snapshot = owner
        try:
            guard = _try_guard(guard_factory)
        except Exception as exc:
            return ShutdownCoordination(
                False,
                f"runtime_guard_open_failed:{type(exc).__name__}",
                owner_pid=owner_pid,
                owner_id=owner_id,
                owner_role=owner_role,
                owner_process_creation_time=owner_creation,
            )
        if guard is not None:
            try:
                return _guarded_previous_owner_result(
                    guard,
                    snapshot,
                    expected_pid=int(expected_pid or 0),
                    process_factory=process_factory,
                    deadline=deadline,
                )
            except Exception as exc:
                return ShutdownCoordination(
                    False,
                    f"runtime_guard_validation_failed:{type(exc).__name__}",
                    owner_pid=owner_pid,
                    owner_id=owner_id,
                    owner_role=owner_role,
                    owner_process_creation_time=owner_creation,
                    lock_acquired=True,
                    guard=guard,
                )
        event.wait(min(0.05, max(0.0, deadline - time.monotonic())))

    return ShutdownCoordination(
        False,
        last_identity_error,
        owner_pid=owner_pid,
        owner_id=owner_id,
        owner_role=owner_role,
        owner_process_creation_time=owner_creation,
    )


def coordinate_runtime_shutdown(
    root: Path,
    timeout: float = 20.0,
    *,
    expected_pid: int = 0,
    lock_factory=None,
    guard_factory=None,
    mailbox=None,
    process_factory=None,
) -> ShutdownCoordination:
    """Return a structured result; ordinary coordination failures never escape."""
    try:
        return _coordinate_runtime_shutdown_impl(
            root,
            timeout,
            expected_pid=expected_pid,
            lock_factory=lock_factory,
            guard_factory=guard_factory,
            mailbox=mailbox,
            process_factory=process_factory,
        )
    except Exception as exc:
        return ShutdownCoordination(False, f"coordination_exception:{type(exc).__name__}")


def request_runtime_shutdown(root: Path, timeout: float = 20.0, *, lock_factory=None, guard_factory=None, mailbox=None, process_factory=None) -> bool:
    result = coordinate_runtime_shutdown(root, timeout, lock_factory=lock_factory, guard_factory=guard_factory, mailbox=mailbox, process_factory=process_factory)
    try:
        return bool(result.ok)
    finally:
        result.release_guard()


def status_path(root: Path) -> Path:
    return root / "data" / "update_last.json"


def write_status(
    root: Path,
    *,
    ok: bool,
    before: str,
    after: str,
    log: Path,
    error: str = "",
    state: str = "",
    remaining_pids: list[int] | None = None,
) -> None:
    path = status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": bool(ok),
        "before": before,
        "after": after,
        "error": str(error or ""),
        "log": str(log),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if state:
        payload["state"] = str(state)
    if remaining_pids:
        payload["remaining_pids"] = sorted({int(pid) for pid in remaining_pids if int(pid) > 0})[:32]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def launch_nova(root: Path) -> tuple[bool, str]:
    try:
        py = select_gui_python(root)
        app = root / "app.py"
        if not app.exists():
            return False, f"No existe {app}"
        subprocess.Popen(
            [str(py), str(app), "--post-update"],
            cwd=str(root),
            creationflags=detached_hidden_creation_flags(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True, f"{py} {app} --post-update"
    except Exception as exc:
        return False, str(exc)


def _show_surviving_runtime(root: Path, result: ShutdownCoordination) -> bool:
    if not result.owner_id or result.owner_role != "runtime":
        return False
    try:
        _observer, _guard, mailbox = _runtime_components(root)
        return bool(mailbox.send("show", target_owner_id=result.owner_id))
    except Exception:
        return False


def run_update(root: Path, log: Path) -> tuple[int, str]:
    py = console_python(root)
    updater = root / "updater" / "nova_updater.py"
    if not updater.exists():
        return 2, f"No existe {updater}"
    cmd = [str(py), str(updater), "--yes"]
    try:
        with open(log, "w", encoding="utf-8", errors="replace") as stream:
            stream.write("Nova Update Runner\n")
            stream.write("Comando: " + subprocess.list2cmdline(cmd) + "\n\n")
            stream.flush()
            proc = subprocess.run(cmd, cwd=str(root), stdout=stream, stderr=subprocess.STDOUT, text=True)
        return int(proc.returncode), ""
    except Exception as exc:
        return 2, str(exc)


def _append_log_best_effort(log: Path, text: str) -> None:
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8", errors="replace") as stream:
            stream.write(str(text))
    except Exception:
        return


def _write_status_best_effort(
    root: Path,
    *,
    ok: bool,
    before: str,
    after: str,
    log: Path,
    error: str,
    state: str = "",
    remaining_pids: list[int] | None = None,
) -> None:
    try:
        write_status(
            root,
            ok=ok,
            before=before,
            after=after,
            log=log,
            error=error,
            state=state,
            remaining_pids=remaining_pids,
        )
    except Exception as exc:
        _append_log_best_effort(log, f"\n[WARN ESTADO] {type(exc).__name__}: {exc}\n")


def _read_version_best_effort(root: Path, fallback: str, log: Path, label: str) -> str:
    try:
        return read_version(root)
    except Exception as exc:
        _append_log_best_effort(log, f"\n[WARN VERSION {label}] {type(exc).__name__}: {exc}\n")
        return fallback


def _read_recovery_best_effort(root: Path) -> dict[str, Any]:
    try:
        path = root / "data" / "update_recovery.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _release_coordination_guard_best_effort(coordination: ShutdownCoordination, log: Path) -> None:
    try:
        coordination.release_guard()
    except Exception as exc:
        _append_log_best_effort(log, f"\n[WARN GUARD] {type(exc).__name__}: {exc}\n")


def _release_supervisor_mutex_best_effort(lock, log: Path | None = None) -> None:
    if lock is None:
        return
    try:
        lock.release()
    except Exception as exc:
        if log is not None:
            _append_log_best_effort(log, f"\n[WARN SUPERVISOR MUTEX] {type(exc).__name__}: {exc}\n")


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

    # Lock ordering invariant:
    # supervisor mutex -> runtime coordination/guard -> update/rollback
    # -> release runtime guard -> launch -> release supervisor mutex.
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
            _write_status_best_effort(
                root,
                ok=False,
                before=before,
                after=before,
                log=log,
                error=error,
                state="coordination_failed",
            )
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
        fail_closed_no_launch = False

        try:
            try:
                rc, runner_error = run_update(root, log)
                rc = int(rc)
            except Exception as exc:
                rc = 2
                runner_error = f"run_update inesperado: {type(exc).__name__}: {exc}"

            fail_closed_no_launch = rc == PIP_TERMINATION_UNCONFIRMED_CODE
            ok = rc == 0
            remaining_pids: list[int] = []
            state = "completed" if ok else "update_failed"
            if fail_closed_no_launch:
                recovery = _read_recovery_best_effort(root)
                state = "pip_termination_unconfirmed"
                remaining_pids = [
                    int(pid) for pid in (recovery.get("remaining_pids") or [])
                    if str(pid).isdigit() and int(pid) > 0
                ][:32]
                error = str(recovery.get("message") or "La terminación de pip no pudo confirmarse; Nova no se relanzará automáticamente.")
            elif runner_error:
                error = str(runner_error)
            elif not ok:
                error = f"El updater terminó con código {rc}. Revisa {log}."

            after = _read_version_best_effort(root, before, log, "DESPUÉS")
            _write_status_best_effort(
                root,
                ok=ok,
                before=before,
                after=after,
                log=log,
                error=error,
                state=state,
                remaining_pids=remaining_pids,
            )
        finally:
            _release_coordination_guard_best_effort(coordination, log)
            if not fail_closed_no_launch:
                launched, _launch_detail = _launch_recovery_once(root, log)
            else:
                _append_log_best_effort(
                    log,
                    "\n[FAIL-CLOSED] pip_termination_unconfirmed: guard liberado después de agotar la escalada; Nova no fue relanzada.\n",
                )

        if fail_closed_no_launch:
            return PIP_TERMINATION_UNCONFIRMED_CODE
        if ok:
            return 0 if launched else 3
        return rc or 2
    finally:
        _release_supervisor_mutex_best_effort(supervisor_mutex, log)


if __name__ == "__main__":
    raise SystemExit(main())
