from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any


class PipContainmentSetupError(RuntimeError):
    """Authoritative containment could not be established before pip execution."""

    dependency_started = False
    authoritative_containment = False


@dataclass(frozen=True)
class PipTerminationResult:
    # First five fields preserve the pre-hardening constructor contract.
    terminated_confirmed: bool
    direct_process_terminated: bool
    remaining_pids: list[int] = field(default_factory=list)
    termination_errors: list[str] = field(default_factory=list)
    detail: str = ""
    dependency_started: bool = True
    authoritative_containment: bool = False
    container_close_requested: bool = False
    remaining_processes: list[dict[str, Any]] = field(default_factory=list)
    rollback_allowed: bool = False
    quarantine_required: bool = False
    identity_inspection_complete: bool = True


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


ULONG_PTR = ctypes.c_size_t
SIZE_T = ctypes.c_size_t
LARGE_INTEGER = ctypes.c_longlong


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", LARGE_INTEGER),
        ("TotalKernelTime", LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", LARGE_INTEGER),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _job_pid_list_type(capacity: int):
    class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ULONG_PTR * int(capacity)),
        ]
    return _JOBOBJECT_BASIC_PROCESS_ID_LIST


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _clean_error(label: str, error: object) -> str:
    text = str(error or "").replace("\r", " ").replace("\n", " ").strip()[:240]
    return f"{label}:{text}" if text else label


class WindowsJobApi:
    """Typed Win32 API surface used to create and own a kill-on-close Job."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectBasicAccountingInformation = 1
    JobObjectBasicProcessIdList = 3
    JobObjectExtendedLimitInformation = 9
    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87
    ERROR_MORE_DATA = 234
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows Job Objects are only available on Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel32
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateJobObject.restype = wintypes.BOOL
        k.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        k.CreateProcessW.restype = wintypes.BOOL
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.ResumeThread.restype = wintypes.DWORD
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

    def last_error(self) -> int:
        return int(ctypes.get_last_error())

    def create_job(self):
        handle = self.kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(self.last_error())
        return handle

    def configure_kill_on_close(self, job) -> None:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self.kernel32.SetInformationJobObject(job, self.JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            raise ctypes.WinError(self.last_error())

    def create_suspended(self, command: list[str], cwd: str | None, *, breakaway: bool) -> _PROCESS_INFORMATION:
        if not command:
            raise ValueError("empty process command")
        cmdline = subprocess.list2cmdline([str(item) for item in command])
        mutable = ctypes.create_unicode_buffer(cmdline)
        startup = _STARTUPINFOW()
        startup.cb = ctypes.sizeof(_STARTUPINFOW)
        info = _PROCESS_INFORMATION()
        flags = self.CREATE_SUSPENDED | self.CREATE_NEW_PROCESS_GROUP
        if breakaway:
            flags |= self.CREATE_BREAKAWAY_FROM_JOB
        ok = self.kernel32.CreateProcessW(
            None,
            ctypes.cast(mutable, wintypes.LPWSTR),
            None,
            None,
            False,
            flags,
            None,
            str(cwd) if cwd else None,
            ctypes.byref(startup),
            ctypes.byref(info),
        )
        if not ok:
            raise ctypes.WinError(self.last_error())
        return info

    def assign(self, job, process) -> None:
        if not self.kernel32.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(self.last_error())

    def resume(self, thread) -> None:
        result = int(self.kernel32.ResumeThread(thread))
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(self.last_error())

    def terminate_process(self, process, code: int = 1) -> None:
        if not self.kernel32.TerminateProcess(process, int(code) & 0xFFFFFFFF):
            error = self.last_error()
            if error != self.ERROR_INVALID_PARAMETER:
                raise ctypes.WinError(error)

    def terminate_job(self, job, code: int = 1) -> None:
        if not self.kernel32.TerminateJobObject(job, int(code) & 0xFFFFFFFF):
            raise ctypes.WinError(self.last_error())

    def wait_process(self, process, timeout: float | None) -> bool:
        millis = 0xFFFFFFFF if timeout is None else min(max(int(float(timeout) * 1000), 0), 0xFFFFFFFE)
        result = int(self.kernel32.WaitForSingleObject(process, millis))
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        raise ctypes.WinError(self.last_error())

    def exit_code(self, process) -> int | None:
        value = wintypes.DWORD()
        if not self.kernel32.GetExitCodeProcess(process, ctypes.byref(value)):
            raise ctypes.WinError(self.last_error())
        return None if int(value.value) == self.STILL_ACTIVE else int(value.value)

    def active_process_count(self, job) -> int:
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        returned = wintypes.DWORD()
        if not self.kernel32.QueryInformationJobObject(job, self.JobObjectBasicAccountingInformation, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned)):
            raise ctypes.WinError(self.last_error())
        return int(info.ActiveProcesses)

    def process_ids(self, job) -> list[int]:
        capacity = 64
        for _ in range(7):
            cls = _job_pid_list_type(capacity)
            info = cls()
            returned = wintypes.DWORD()
            ok = self.kernel32.QueryInformationJobObject(job, self.JobObjectBasicProcessIdList, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned))
            if ok:
                count = min(int(info.NumberOfProcessIdsInList), capacity)
                return [int(info.ProcessIdList[index]) for index in range(count) if int(info.ProcessIdList[index]) > 0]
            error = self.last_error()
            if error != self.ERROR_MORE_DATA:
                raise ctypes.WinError(error)
            capacity = max(capacity * 2, int(getattr(info, "NumberOfAssignedProcesses", 0) or 0) + 16)
        raise RuntimeError("job_process_list_too_large")

    def process_creation_time(self, pid: int) -> int | None:
        handle = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            error = self.last_error()
            if error == self.ERROR_INVALID_PARAMETER:
                return None
            raise ctypes.WinError(error)
        try:
            created = _FILETIME()
            exited = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            if not self.kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                raise ctypes.WinError(self.last_error())
            value = _filetime_value(created)
            return value if value > 0 else None
        finally:
            self.close_handle(handle)

    def close_handle(self, handle) -> None:
        if handle:
            if not self.kernel32.CloseHandle(handle):
                raise ctypes.WinError(self.last_error())


class WindowsJobProcess:
    """Process created suspended, assigned to a kill-on-close Job, then resumed."""

    authoritative_containment = True
    dependency_started = True

    def __init__(self, api: WindowsJobApi, job_handle, process_handle, pid: int):
        self.api = api
        self.job_handle = job_handle
        self.process_handle = process_handle
        self.pid = int(pid)
        self.returncode: int | None = None
        self.container_close_requested = False
        self.closed = False
        self.startup_errors: list[str] = []

    @classmethod
    def launch(cls, command: list[str], *, cwd: str | None = None, api: WindowsJobApi | None = None):
        native = api or WindowsJobApi()
        job = None
        info = None
        try:
            job = native.create_job()
            native.configure_kill_on_close(job)
            try:
                info = native.create_suspended(command, cwd, breakaway=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) != native.ERROR_ACCESS_DENIED:
                    raise
                # Some CI/enterprise parents disallow breakaway. Windows 8+
                # can support nested jobs; retry suspended without breakaway.
                info = native.create_suspended(command, cwd, breakaway=False)
            try:
                native.assign(job, info.hProcess)
            except Exception as exc:
                # The process is still suspended: no pip code has run.
                try:
                    native.terminate_process(info.hProcess, 0xE001)
                except Exception:
                    pass
                try:
                    native.wait_process(info.hProcess, 2.0)
                except Exception:
                    pass
                raise PipContainmentSetupError(
                    f"AssignProcessToJobObject failed before pip execution: {type(exc).__name__}"
                ) from exc
            try:
                native.resume(info.hThread)
            except Exception as exc:
                try:
                    native.terminate_job(job, 0xE002)
                except Exception:
                    pass
                try:
                    native.wait_process(info.hProcess, 2.0)
                except Exception:
                    pass
                raise PipContainmentSetupError(
                    f"ResumeThread failed after containment setup: {type(exc).__name__}"
                ) from exc
            startup_errors: list[str] = []
            try:
                native.close_handle(info.hThread)
            except Exception as exc:
                # The process is already running inside the Job. A thread handle
                # cleanup error must not be misreported as a pre-launch failure.
                startup_errors.append(_clean_error("primary_thread_close", type(exc).__name__))
            finally:
                info.hThread = None
            process = cls(native, job, info.hProcess, int(info.dwProcessId))
            process.startup_errors = startup_errors
            job = None
            info.hProcess = None
            return process
        except PipContainmentSetupError:
            raise
        except Exception as exc:
            raise PipContainmentSetupError(
                f"Windows Job Object setup failed before pip execution: {type(exc).__name__}"
            ) from exc
        finally:
            if info is not None:
                if getattr(info, "hThread", None):
                    try:
                        native.close_handle(info.hThread)
                    except Exception:
                        pass
                if getattr(info, "hProcess", None):
                    try:
                        native.close_handle(info.hProcess)
                    except Exception:
                        pass
            if job:
                try:
                    native.close_handle(job)
                except Exception:
                    pass

    def wait(self, timeout=None):
        if not self.api.wait_process(self.process_handle, timeout):
            raise subprocess.TimeoutExpired(["pip"], timeout)
        code = self.api.exit_code(self.process_handle)
        self.returncode = int(code or 0)
        return self.returncode

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if not self.api.wait_process(self.process_handle, 0.0):
            return None
        code = self.api.exit_code(self.process_handle)
        self.returncode = int(code or 0)
        return self.returncode

    def terminate(self):
        self.api.terminate_process(self.process_handle, 1)

    def kill(self):
        self.api.terminate_job(self.job_handle, 1)

    def active_process_count(self) -> int:
        return self.api.active_process_count(self.job_handle)

    def wait_job_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        event = threading.Event()
        while True:
            if self.active_process_count() == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            event.wait(min(0.05, remaining))

    def remaining_identities(self) -> tuple[list[dict[str, Any]], list[str]]:
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            pids = self.api.process_ids(self.job_handle)
        except Exception as exc:
            return [], [_clean_error("job_process_ids", type(exc).__name__)]
        for pid in pids:
            try:
                creation = self.api.process_creation_time(pid)
                if creation:
                    rows.append({
                        "pid": int(pid),
                        "creation_time": int(creation),
                        "role": "pip_root_or_descendant",
                    })
                else:
                    errors.append(f"process_identity_gone:{int(pid)}")
            except Exception as exc:
                # PID only is never persisted as a strong identity. The error
                # keeps recovery fail-closed until an operator can inspect it.
                errors.append(_clean_error(f"process_identity:{int(pid)}", type(exc).__name__))
        return rows, errors

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        first_error = None
        if self.job_handle:
            self.container_close_requested = True
            try:
                self.api.close_handle(self.job_handle)
            except Exception as exc:
                first_error = exc
            finally:
                self.job_handle = None
        if self.process_handle:
            try:
                self.api.close_handle(self.process_handle)
            except Exception as exc:
                first_error = first_error or exc
            finally:
                self.process_handle = None
        if first_error is not None:
            raise first_error


class PsutilProcessTree:
    """Non-Windows fallback/diagnostic process-tree helper."""

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
            for row in self.psutil.process_iter(["pid"]):
                pid = int(row.info.get("pid") or 0)
                if pid <= 0:
                    continue
                try:
                    if os.getpgid(pid) == root_pid:
                        pids.add(pid)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
        return pids

    def identity(self, pid: int) -> dict[str, Any] | None:
        try:
            process = self.psutil.Process(int(pid))
            creation = int(float(process.create_time()) * 1_000_000)
            return {"pid": int(pid), "creation_time": creation, "role": "pip_root_or_descendant"}
        except self.psutil.NoSuchProcess:
            return None

    def terminate(self, root_pid: int, pids: set[int]) -> None:
        try:
            os.killpg(int(root_pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self, root_pid: int, pids: set[int]) -> None:
        try:
            os.killpg(int(root_pid), signal.SIGKILL)
        except ProcessLookupError:
            pass

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


def launch_pip_process(command: list[str], *, cwd: str | None = None, api=None):
    if os.name == "nt":
        return WindowsJobProcess.launch(command, cwd=cwd, api=api)
    return subprocess.Popen(command, cwd=cwd, start_new_session=True)


def pip_popen_kwargs() -> dict[str, Any]:
    """Compatibility helper retained for callers/tests outside the new launcher."""
    if os.name == "nt":
        # A raw Popen is no longer authoritative on Windows. Production code
        # must use ``launch_pip_process`` instead.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _wait_direct(proc, timeout: float, errors: list[str], label: str) -> bool:
    try:
        proc.wait(timeout=max(0.0, float(timeout)))
        return True
    except subprocess.TimeoutExpired:
        errors.append(f"{label}:TimeoutExpired")
        return False
    except Exception as exc:
        errors.append(_clean_error(label, type(exc).__name__))
        return False


def _direct_is_terminated(proc) -> bool:
    try:
        return proc.poll() is not None
    except Exception:
        return False


def _terminate_windows_job(proc: WindowsJobProcess, grace_seconds: float) -> PipTerminationResult:
    errors: list[str] = list(getattr(proc, "startup_errors", []) or [])
    direct_terminated = False
    confirmed = False
    remaining: list[dict[str, Any]] = []
    identity_errors: list[str] = []
    try:
        try:
            proc.terminate()
        except Exception as exc:
            errors.append(_clean_error("terminate_direct", type(exc).__name__))
        direct_terminated = _wait_direct(proc, grace_seconds, errors, "wait_after_terminate") or _direct_is_terminated(proc)
        try:
            empty = proc.wait_job_empty(grace_seconds)
        except Exception as exc:
            empty = False
            errors.append(_clean_error("job_empty_after_terminate", type(exc).__name__))
        if not empty or not direct_terminated:
            try:
                proc.kill()
            except Exception as exc:
                errors.append(_clean_error("terminate_job", type(exc).__name__))
            direct_terminated = _wait_direct(proc, grace_seconds, errors, "wait_after_job_terminate") or _direct_is_terminated(proc)
            try:
                empty = proc.wait_job_empty(grace_seconds)
            except Exception as exc:
                empty = False
                errors.append(_clean_error("job_empty_final", type(exc).__name__))
        if not empty:
            remaining, identity_errors = proc.remaining_identities()
            errors.extend(identity_errors)
        confirmed = bool(proc.authoritative_containment and direct_terminated and empty and not identity_errors)
    finally:
        try:
            proc.close()
        except Exception as exc:
            errors.append(_clean_error("job_close", type(exc).__name__))
            confirmed = False
    pids = sorted({int(row["pid"]) for row in remaining if int(row.get("pid") or 0) > 0})
    detail = "terminación autoritativa del Job Object confirmada" if confirmed else "terminación del Job Object no confirmada"
    return PipTerminationResult(
        terminated_confirmed=confirmed,
        direct_process_terminated=direct_terminated,
        remaining_pids=pids,
        termination_errors=errors[:128],
        detail=detail,
        dependency_started=True,
        authoritative_containment=True,
        container_close_requested=bool(proc.container_close_requested),
        remaining_processes=remaining[:128],
        rollback_allowed=confirmed,
        quarantine_required=not confirmed,
        identity_inspection_complete=not bool(identity_errors),
    )


def _terminate_posix_tree(proc, grace_seconds: float, *, tree_api=None) -> PipTerminationResult:
    api = tree_api or PsutilProcessTree()
    root_pid = int(getattr(proc, "pid", 0) or 0)
    errors: list[str] = []
    known: set[int] = {root_pid} if root_pid > 0 else set()
    inspection_ok = root_pid > 0

    def snapshot(label: str):
        nonlocal inspection_ok
        try:
            known.update(int(pid) for pid in api.snapshot(root_pid) if int(pid) > 0)
        except Exception as exc:
            errors.append(_clean_error(label, type(exc).__name__))
            inspection_ok = False

    snapshot("snapshot_initial")
    try:
        proc.terminate()
    except Exception as exc:
        errors.append(_clean_error("terminate_direct", type(exc).__name__))
    if root_pid > 0:
        try:
            api.terminate(root_pid, set(known))
        except Exception as exc:
            errors.append(_clean_error("terminate_tree", type(exc).__name__))
    direct = _wait_direct(proc, grace_seconds, errors, "wait_after_terminate")
    snapshot("snapshot_after_terminate")
    try:
        remaining_pids = set(api.alive(set(known)))
    except Exception as exc:
        errors.append(_clean_error("alive_after_terminate", type(exc).__name__))
        inspection_ok = False
        remaining_pids = set(known)
    if remaining_pids or not direct:
        try:
            proc.kill()
        except Exception as exc:
            errors.append(_clean_error("kill_direct", type(exc).__name__))
        snapshot("snapshot_before_kill")
        try:
            api.kill(root_pid, set(known))
        except Exception as exc:
            errors.append(_clean_error("kill_tree", type(exc).__name__))
        direct = _wait_direct(proc, grace_seconds, errors, "wait_after_kill") or _direct_is_terminated(proc)
    snapshot("snapshot_final")
    try:
        remaining_pids = set(api.alive(set(known)))
    except Exception as exc:
        errors.append(_clean_error("alive_final", type(exc).__name__))
        inspection_ok = False
        remaining_pids = set(known)
    direct = bool(direct or _direct_is_terminated(proc))
    if direct:
        remaining_pids.discard(root_pid)
    elif root_pid > 0:
        remaining_pids.add(root_pid)
    identities: list[dict[str, Any]] = []
    for pid in sorted(remaining_pids):
        try:
            identity = api.identity(pid) if hasattr(api, "identity") else None
            if identity:
                identities.append(identity)
            else:
                inspection_ok = False
                errors.append(f"process_identity_unavailable:{pid}")
        except Exception as exc:
            inspection_ok = False
            errors.append(_clean_error(f"process_identity:{pid}", type(exc).__name__))
    confirmed = bool(inspection_ok and direct and not remaining_pids)
    return PipTerminationResult(
        confirmed,
        direct,
        sorted(int(pid) for pid in remaining_pids)[:64],
        errors[:128],
        "terminación de sesión/grupo confirmada" if confirmed else "terminación de sesión/grupo no confirmada",
        dependency_started=True,
        authoritative_containment=confirmed,
        container_close_requested=True,
        remaining_processes=identities[:128],
        rollback_allowed=confirmed,
        quarantine_required=not confirmed,
        identity_inspection_complete=inspection_ok,
    )


def terminate_pip_tree(proc, grace_seconds: float, *, tree_api=None) -> PipTerminationResult:
    """Terminate and verify pip without using PID snapshots as Windows proof."""
    if isinstance(proc, WindowsJobProcess):
        return _terminate_windows_job(proc, grace_seconds)
    if os.name == "nt" and tree_api is None:
        # Production Windows callers must never silently degrade to psutil/PID
        # tree inspection when Job containment is absent.
        try:
            proc.close()
        except Exception:
            pass
        pid = int(getattr(proc, "pid", 0) or 0)
        return PipTerminationResult(
            False,
            _direct_is_terminated(proc),
            [pid] if pid > 0 else [],
            ["windows_authoritative_job_missing"],
            "Windows Job Object containment missing",
            dependency_started=True,
            authoritative_containment=False,
            container_close_requested=False,
            remaining_processes=[],
            rollback_allowed=False,
            quarantine_required=True,
            identity_inspection_complete=False,
        )
    return _terminate_posix_tree(proc, grace_seconds, tree_api=tree_api)


def verify_normal_completion(proc, grace_seconds: float) -> PipTerminationResult | None:
    """Ensure a successful root exit did not leave contained descendants alive."""
    if isinstance(proc, WindowsJobProcess):
        errors: list[str] = []
        try:
            empty = proc.wait_job_empty(grace_seconds)
        except Exception as exc:
            empty = False
            errors.append(_clean_error("job_empty_after_normal_exit", type(exc).__name__))
        if empty:
            direct = _direct_is_terminated(proc)
            try:
                proc.close()
            except Exception as exc:
                errors.append(_clean_error("job_close", type(exc).__name__))
                direct = False
            confirmed = bool(direct and not errors)
            return PipTerminationResult(
                confirmed,
                direct,
                [],
                errors,
                "normal Job Object completion confirmed" if confirmed else "normal Job Object completion not confirmed",
                dependency_started=True,
                authoritative_containment=True,
                container_close_requested=bool(proc.container_close_requested),
                remaining_processes=[],
                rollback_allowed=confirmed,
                quarantine_required=not confirmed,
            )
        # Descendants lingered or inspection failed; terminate the whole job and
        # return a structured result so the transaction rolls back/quarantines.
        return _terminate_windows_job(proc, grace_seconds)
    return None
