from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path

def _hash_text(value: str, length: int = 24) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:length]


class _ScopedFileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.stream = None
        self.acquired = False

    def acquire(self) -> bool:
        if self.acquired:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            stream.close()
            return False
        self.stream = stream
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired or self.stream is None:
            return
        stream = self.stream
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self.stream = None
            self.acquired = False


def _windows_scope_identity() -> tuple[str, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_uint, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID)).contents.value
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = str(sid_text.value or "")
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        if not sid:
            raise RuntimeError("empty_user_sid")
        return _hash_text(sid), int(session.value)
    finally:
        kernel32.CloseHandle(token)


def _scope_directory() -> Path:
    if os.name == "nt":
        try:
            user_hash, session_id = _windows_scope_identity()
        except Exception:
            raw = "|".join((os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""), str(Path.home())))
            user_hash = _hash_text(raw)
            session_id = int(_hash_text(os.environ.get("SESSIONNAME") or "default", 8), 16)
    else:
        raw = "|".join((os.environ.get("USER", ""), os.environ.get("USERNAME", ""), os.environ.get("HOME", "")))
        user_hash = _hash_text(raw or str(Path.home()))
        session = os.environ.get("XDG_SESSION_ID") or os.environ.get("SESSIONNAME") or "default"
        session_id = int(_hash_text(session, 8), 16)
    scope_id = _hash_text(f"{user_hash}|{session_id}")
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".nova")) / "Nova" / "runtime"
    return base / f"scope-{scope_id}"


def _supervisor_lock() -> _ScopedFileLock:
    return _ScopedFileLock(_scope_directory() / "update_supervisor.lock")


def _runtime_guard() -> _ScopedFileLock:
    return _ScopedFileLock(_scope_directory() / "runtime.lock")
