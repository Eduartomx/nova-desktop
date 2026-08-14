from __future__ import annotations

import argparse
from dataclasses import dataclass
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


class ProcessCapture:
    """A stable reference to the owner process.

    Windows uses a real process HANDLE captured before shutdown is requested,
    preventing PID reuse from being mistaken for the original runtime.
    """

    def __init__(self, pid: int, handle=None, *, already_terminated=False):
        self.pid = int(pid)
        self.handle = handle
        self.already_terminated = bool(already_terminated)

    @classmethod
    def open(cls, pid: int):
        pid = int(pid)
        if pid <= 0:
            raise ValueError("invalid pid")
        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, OSError):
                return cls(pid, already_terminated=True)
            return cls(pid)

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            # ERROR_INVALID_PARAMETER: PID no longer exists.
            if error == 87:
                return cls(pid, already_terminated=True)
            raise ctypes.WinError(error)
        capture = cls(pid, handle)
        capture._kernel32 = kernel32
        return capture

    def wait(self, timeout: float) -> bool:
        if self.already_terminated:
            return True
        timeout = max(0.0, float(timeout))
        if os.name == "nt" and self.handle:
            WAIT_OBJECT_0 = 0
            WAIT_TIMEOUT = 258
            millis = min(int(timeout * 1000), 0xFFFFFFFE)
            result = int(self._kernel32.WaitForSingleObject(self.handle, millis))
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
    command_sent: bool = False
    process_terminated: bool = False
    lock_acquired: bool = False


def _runtime_components(root: Path, *, lock_factory=None, mailbox=None):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from assistant.instance_lock import InstanceLock, runtime_paths
    from assistant.instance_commands import InstanceCommandMailbox

    paths = runtime_paths()
    if lock_factory is None:
        lock_factory = lambda: InstanceLock(path=paths.lock, owner_path=paths.owner)
    if mailbox is None:
        mailbox = InstanceCommandMailbox(paths.commands)
    return lock_factory, mailbox


def _probe_lock(lock_factory) -> bool:
    probe = lock_factory()
    if not probe.acquire():
        return False
    probe.release()
    return True


def coordinate_runtime_shutdown(
    root: Path,
    timeout: float = 20.0,
    *,
    expected_pid: int = 0,
    lock_factory=None,
    mailbox=None,
    process_factory=None,
) -> ShutdownCoordination:
    """Authorize updating only after BOTH owner death and lock reacquisition."""
    lock_factory, mailbox = _runtime_components(root, lock_factory=lock_factory, mailbox=mailbox)
    process_factory = process_factory or ProcessCapture.open
    deadline = time.monotonic() + max(0.0, float(timeout))

    # No current runtime: lock is already obtainable, therefore there is no
    # owner process that could be executing managed Nova files.
    if _probe_lock(lock_factory):
        return ShutdownCoordination(True, process_terminated=True, lock_acquired=True)

    observer = lock_factory()
    owner = observer.read_owner()
    if not owner:
        return ShutdownCoordination(False, "runtime_owner_metadata_unavailable")
    owner_pid = int(owner.get("pid") or 0)
    owner_id = str(owner.get("owner_id") or "")
    if expected_pid and owner_pid != int(expected_pid):
        return ShutdownCoordination(False, "runtime_owner_pid_mismatch", owner_pid=owner_pid, owner_id=owner_id)

    try:
        captured = process_factory(owner_pid)
    except Exception as exc:
        return ShutdownCoordination(False, f"owner_process_capture_failed:{type(exc).__name__}", owner_pid=owner_pid, owner_id=owner_id)

    try:
        # Re-read after opening the HANDLE. If ownership changed in that tiny
        # window, the captured process is not the generation we intend to stop.
        verify = observer.read_owner()
        if verify:
            if int(verify.get("pid") or 0) != owner_pid or str(verify.get("owner_id") or "") != owner_id:
                return ShutdownCoordination(False, "runtime_owner_changed_during_capture", owner_pid=owner_pid, owner_id=owner_id)

        command_sent = False
        if not getattr(captured, "already_terminated", False):
            command_sent = bool(mailbox.send("shutdown_for_update", target_owner_id=owner_id))
            if not command_sent:
                return ShutdownCoordination(False, "shutdown_command_delivery_failed", owner_pid=owner_pid, owner_id=owner_id)

        remaining = max(0.0, deadline - time.monotonic())
        try:
            process_terminated = bool(captured.wait(remaining))
        except Exception as exc:
            return ShutdownCoordination(False, f"owner_process_wait_failed:{type(exc).__name__}", owner_pid=owner_pid, owner_id=owner_id, command_sent=command_sent)
        if not process_terminated:
            return ShutdownCoordination(False, "owner_process_timeout", owner_pid=owner_pid, owner_id=owner_id, command_sent=command_sent)
    finally:
        try:
            captured.close()
        except Exception:
            pass

    event = threading.Event()
    while time.monotonic() <= deadline:
        if _probe_lock(lock_factory):
            return ShutdownCoordination(
                True,
                owner_pid=owner_pid,
                owner_id=owner_id,
                command_sent=command_sent,
                process_terminated=True,
                lock_acquired=True,
            )
        event.wait(min(0.05, max(0.0, deadline - time.monotonic())))
    return ShutdownCoordination(
        False,
        "runtime_lock_timeout_after_process_exit",
        owner_pid=owner_pid,
        owner_id=owner_id,
        command_sent=command_sent,
        process_terminated=True,
        lock_acquired=False,
    )


def request_runtime_shutdown(root: Path, timeout: float = 20.0, *, lock_factory=None, mailbox=None, process_factory=None) -> bool:
    return bool(
        coordinate_runtime_shutdown(
            root,
            timeout,
            lock_factory=lock_factory,
            mailbox=mailbox,
            process_factory=process_factory,
        ).ok
    )


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
    if not result.owner_id:
        return False
    try:
        _lock_factory, mailbox = _runtime_components(root)
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
        # If the original runtime is still alive, restore visibility through the
        # same owner-targeted IPC instead of launching a competing runtime.
        _show_surviving_runtime(root, coordination)
        return 4

    rc, runner_error = run_update(root, log)
    after = read_version(root)
    ok = rc == 0
    error = runner_error or ("" if ok else f"El updater terminó con código {rc}. Revisa {log}.")
    write_status(root, ok=ok, before=before, after=after, log=log, error=error)

    # At this point the previous owner is confirmed dead and its lock is free.
    # Exactly one visible post-update process is launched on both success/error.
    launched, launch_detail = launch_nova(root)
    if not launched:
        with open(log, "a", encoding="utf-8", errors="replace") as stream:
            stream.write("\n[ERROR REINICIO] " + launch_detail + "\n")
        return 3 if ok else rc or 2
    return 0 if ok else rc or 2


if __name__ == "__main__":
    raise SystemExit(main())
