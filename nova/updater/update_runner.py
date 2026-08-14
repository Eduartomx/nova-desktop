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


def nova_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_version(root: Path) -> str:
    path = root / "NOVA_VERSION.txt"
    if not path.exists():
        return "0.0.0"
    return path.read_text(encoding="utf-8", errors="ignore").strip().lstrip("vV") or "0.0.0"


def console_python(root: Path) -> Path:
    candidate = root / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return candidate
    current = Path(sys.executable)
    if current.name.casefold() == "pythonw.exe":
        sibling = current.with_name("python.exe")
        if sibling.exists():
            return sibling
    return current


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
            # Preserve previous owner.json until the kernel lock has been taken
            # and the previous process identity has been validated under it.
            guard_factory = lambda: InstanceLock(path=paths.lock, owner_path=paths.owner, publish_owner=False, role="updater")
    if mailbox is None:
        mailbox = InstanceCommandMailbox(paths.commands)
    return lock_factory, guard_factory, mailbox


def _try_guard(guard_factory):
    guard = guard_factory()
    if guard.acquire():
        return guard
    return None


def _owner_values(owner: dict | None) -> tuple[int, str, str, int]:
    owner = owner or {}
    return (
        int(owner.get("pid") or 0),
        str(owner.get("owner_id") or ""),
        str(owner.get("role") or "runtime"),
        int(owner.get("process_creation_time") or 0),
    )


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


def _guarded_previous_owner_result(
    guard,
    owner_snapshot: dict | None,
    *,
    expected_pid: int,
    process_factory,
    deadline: float,
) -> ShutdownCoordination:
    """Validate the process represented by metadata while *guard* is held.

    ``guard`` is deliberately not published yet, so ``owner_snapshot`` remains
    the previous generation evidence. A live process without a creation marker
    is never waited on or assumed to be the old Nova process.
    """
    owner_pid, owner_id, owner_role, owner_creation = _owner_values(owner_snapshot)
    target_pid = int(expected_pid or owner_pid or 0)
    target_creation = owner_creation if target_pid > 0 and target_pid == owner_pid else 0
    process_terminated = target_pid <= 0

    if target_pid > 0:
        try:
            captured = process_factory(target_pid)
        except Exception as exc:
            guard.release()
            return ShutdownCoordination(
                False,
                f"owner_process_capture_failed:{type(exc).__name__}",
                owner_pid=target_pid,
                owner_id=owner_id,
                owner_role=owner_role,
                owner_process_creation_time=target_creation,
                lock_acquired=True,
            )
        try:
            if getattr(captured, "already_terminated", False):
                process_terminated = True
            elif target_creation <= 0:
                # Legacy/no metadata cannot distinguish the old process from PID
                # reuse. Fail closed rather than waiting on an unrelated process.
                guard.release()
                return ShutdownCoordination(
                    False,
                    "runtime_owner_identity_unavailable",
                    owner_pid=target_pid,
                    owner_id=owner_id,
                    owner_role=owner_role,
                    owner_process_creation_time=0,
                    process_terminated=False,
                    lock_acquired=True,
                )
            elif not captured.matches_creation_time(target_creation):
                # Same PID, different process creation time: the previous owner
                # is gone and this PID belongs to an unrelated process.
                process_terminated = True
            else:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    process_terminated = bool(captured.wait(remaining))
                except Exception as exc:
                    guard.release()
                    return ShutdownCoordination(
                        False,
                        f"owner_process_wait_failed:{type(exc).__name__}",
                        owner_pid=target_pid,
                        owner_id=owner_id,
                        owner_role=owner_role,
                        owner_process_creation_time=target_creation,
                        lock_acquired=True,
                    )
                if not process_terminated:
                    guard.release()
                    return ShutdownCoordination(
                        False,
                        "owner_process_timeout",
                        owner_pid=target_pid,
                        owner_id=owner_id,
                        owner_role=owner_role,
                        owner_process_creation_time=target_creation,
                        lock_acquired=True,
                    )
        finally:
            try:
                captured.close()
            except Exception:
                pass

    published, publish_error = _publish_updater_guard(guard)
    if not published:
        guard.release()
        return ShutdownCoordination(
            False,
            publish_error,
            owner_pid=target_pid or owner_pid,
            owner_id=owner_id,
            owner_role=owner_role if owner_snapshot else "none",
            owner_process_creation_time=target_creation,
            process_terminated=process_terminated,
            lock_acquired=True,
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
    event = threading.Event()
    last = None
    while time.monotonic() <= deadline:
        # Snapshot before acquiring. Once the guard succeeds, no later owner can
        # acquire the kernel lock and this snapshot is stable evidence of the
        # generation that most recently released it.
        snapshot = observer.read_owner()
        guard = _try_guard(guard_factory)
        if guard is not None:
            return _guarded_previous_owner_result(
                guard,
                snapshot,
                expected_pid=0,
                process_factory=process_factory,
                deadline=deadline,
            )
        last = snapshot
        event.wait(min(0.05, max(0.0, deadline - time.monotonic())))
    pid, owner_id, role, creation = _owner_values(last)
    return ShutdownCoordination(
        False,
        "runtime_lock_timeout_after_process_exit",
        owner_pid=pid,
        owner_id=owner_id,
        owner_role=role,
        owner_process_creation_time=creation,
        process_terminated=True,
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
    """Return only with the previous process dead and an updater guard held."""
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

    # Acquire the real guard directly; never probe and release before updating.
    # Because the default guard is initially unpublished, previous owner.json is
    # still available for validation under exclusive lock.
    guard = _try_guard(guard_factory)
    if guard is not None:
        stable_snapshot = observer.read_owner() or owner_snapshot
        return _guarded_previous_owner_result(
            guard,
            stable_snapshot,
            expected_pid=int(expected_pid or 0),
            process_factory=process_factory,
            deadline=deadline,
        )

    # Lock is occupied. Strong metadata is mandatory before sending a command or
    # waiting on a process identity.
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
                        command_sent = bool(mailbox.send("shutdown_for_update", target_owner_id=owner_id))
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
                        # Keep the identity/command that initiated this shutdown in
                        # the diagnostic result even if a competing owner won.
                        guarded.command_sent = command_sent
                        if not guarded.owner_pid:
                            guarded.owner_pid = owner_pid
                            guarded.owner_id = owner_id
                            guarded.owner_role = owner_role
                            guarded.owner_process_creation_time = owner_creation
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

        # Metadata can lag a crashed owner. If the kernel lock becomes free,
        # acquire it without overwriting evidence, then validate that evidence.
        snapshot = owner
        guard = _try_guard(guard_factory)
        if guard is not None:
            return _guarded_previous_owner_result(
                guard,
                snapshot,
                expected_pid=int(expected_pid or 0),
                process_factory=process_factory,
                deadline=deadline,
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


def request_runtime_shutdown(root: Path, timeout: float = 20.0, *, lock_factory=None, guard_factory=None, mailbox=None, process_factory=None) -> bool:
    result = coordinate_runtime_shutdown(root, timeout, lock_factory=lock_factory, guard_factory=guard_factory, mailbox=mailbox, process_factory=process_factory)
    try:
        return bool(result.ok)
    finally:
        result.release_guard()


def status_path(root: Path) -> Path:
    return root / "data" / "update_last.json"


def write_status(root: Path, *, ok: bool, before: str, after: str, log: Path, error: str = "") -> None:
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def launch_nova(root: Path) -> tuple[bool, str]:
    try:
        pyw = root / ".venv" / "Scripts" / "pythonw.exe"
        py = pyw if pyw.exists() else console_python(root)
        app = root / "app.py"
        if not app.exists():
            return False, f"No existe {app}"
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [str(py), str(app), "--post-update"],
            cwd=str(root),
            creationflags=flags,
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Supervisa una actualización de Nova y relanza la aplicación.")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)
    root = nova_root()
    logs = root / "data" / "updater_logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / ("update_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
    before = read_version(root)

    coordination = coordinate_runtime_shutdown(root, args.wait_seconds, expected_pid=args.parent_pid)
    if not coordination.ok:
        error = "Nova no terminó de forma verificable; no se modificaron archivos. " + coordination.error
        write_status(root, ok=False, before=before, after=before, log=log, error=error)
        _show_surviving_runtime(root, coordination)
        return 4

    try:
        rc, runner_error = run_update(root, log)
        after = read_version(root)
        ok = rc == 0
        error = runner_error or ("" if ok else f"El updater terminó con código {rc}. Revisa {log}.")
        write_status(root, ok=ok, before=before, after=after, log=log, error=error)
    finally:
        # The updater guard covers the complete update/rollback transaction and
        # is released only immediately before the one visible post-update launch.
        coordination.release_guard()

    launched, launch_detail = launch_nova(root)
    if not launched:
        with open(log, "a", encoding="utf-8", errors="replace") as stream:
            stream.write("\n[ERROR REINICIO] " + launch_detail + "\n")
        return 3 if ok else rc or 2
    return 0 if ok else rc or 2


if __name__ == "__main__":
    raise SystemExit(main())
