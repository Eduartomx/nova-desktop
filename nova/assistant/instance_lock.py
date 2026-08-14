from __future__ import annotations

"""Kernel-backed single-instance ownership for one Nova runtime per user session.

The kernel lock is authoritative. ``owner.json`` identifies the last published
owner generation. Runtime owners publish metadata; updater probes/guards may
first acquire the same kernel lock without overwriting the previous metadata,
then publish an explicit ``updater`` role only after validating that evidence.
Raw Windows SIDs are never persisted.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash_text(value: str, length: int = 24) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:length]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def process_creation_time(pid: int | None = None) -> int | None:
    """Return a stable creation marker for *pid*.

    On Windows this is the native process creation FILETIME in 100 ns units.
    It is suitable for distinguishing PID reuse and contains no user data.
    """
    target = int(pid or os.getpid())
    if target <= 0:
        return None
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
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
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: process no longer exists.
                return None
            raise ctypes.WinError(error)
        try:
            created = _FILETIME()
            exited = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                raise ctypes.WinError(ctypes.get_last_error())
            marker = _filetime_value(created)
            return marker if marker > 0 else None
        finally:
            kernel32.CloseHandle(handle)
    try:
        import psutil
        marker = int(float(psutil.Process(target).create_time()) * 1_000_000)
        return marker if marker > 0 else None
    except Exception:
        return None


def _windows_identity() -> tuple[str, int]:
    if os.name != "nt":
        raw = "|".join((os.environ.get("USER", ""), os.environ.get("USERNAME", ""), os.environ.get("HOME", "")))
        session = os.environ.get("XDG_SESSION_ID") or os.environ.get("SESSIONNAME") or "default"
        return _hash_text(raw or str(Path.home())), int(_hash_text(session, 8), 16)

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

    session_id = wintypes.DWORD(0)
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session_id)):
        raise ctypes.WinError(ctypes.get_last_error())
    TOKEN_QUERY = 0x0008
    TokenUser = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buffer, needed, ctypes.byref(needed)):
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
            raise RuntimeError("empty user SID")
        return _hash_text(sid), int(session_id.value)
    finally:
        kernel32.CloseHandle(token)


def runtime_identity() -> dict[str, Any]:
    try:
        user_hash, session_id = _windows_identity()
        source = "windows_sid_session"
    except Exception:
        user_raw = "|".join((os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""), str(Path.home())))
        session_raw = os.environ.get("SESSIONNAME") or os.environ.get("XDG_SESSION_ID") or "default"
        user_hash = _hash_text(user_raw)
        session_id = int(_hash_text(session_raw, 8), 16)
        source = "profile_session_fallback"
    scope_id = _hash_text(f"{user_hash}|{session_id}")
    return {"user_hash": user_hash, "session_id": int(session_id), "scope_id": scope_id, "identity_source": source}


def _scope_id() -> str:
    return str(runtime_identity()["scope_id"])


@dataclass(frozen=True)
class RuntimePaths:
    base: Path
    scope_id: str
    directory: Path
    lock: Path
    owner: Path
    commands: Path


def runtime_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".nova"))
    return base / "Nova" / "runtime"


def runtime_paths(base: Path | None = None) -> RuntimePaths:
    identity = runtime_identity()
    root = Path(base) if base is not None else runtime_directory()
    scope_id = str(identity["scope_id"])
    directory = root / f"scope-{scope_id}"
    return RuntimePaths(root, scope_id, directory, directory / "runtime.lock", directory / "owner.json", directory / "commands")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class InstanceLock:
    def __init__(
        self,
        *,
        path: Path | None = None,
        owner_path: Path | None = None,
        locker=None,
        publish_owner: bool = True,
        role: str = "runtime",
    ):
        paths = runtime_paths()
        self.path = Path(path) if path is not None else paths.lock
        self.owner_path = Path(owner_path) if owner_path is not None else (paths.owner if path is None else self.path.with_name("owner.json"))
        self._locker = locker
        self._file = None
        self.acquired = False
        self.owner_id = ""
        self.release_requested = False
        self.publish_owner = bool(publish_owner)
        self.role = str(role or "runtime")[:32]
        self.identity = runtime_identity()
        self.process_creation_time = 0

    def _lock_stream(self, stream) -> bool:
        if self._locker is not None:
            return bool(self._locker.acquire(stream))
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True

    def _unlock_stream(self, stream) -> None:
        if self._locker is not None:
            self._locker.release(stream)
            return
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def publish_owner_metadata(self) -> dict[str, Any]:
        """Publish this already-acquired lock as the current owner generation."""
        if not self.acquired or self._file is None:
            raise RuntimeError("cannot publish metadata without acquired lock")
        if self.owner_id and self.process_creation_time:
            current = self.read_owner()
            if current and str(current.get("owner_id") or "") == self.owner_id:
                return current
        owner_id = uuid.uuid4().hex
        marker = process_creation_time(os.getpid())
        if not marker:
            raise RuntimeError("could not determine owner process creation time")
        metadata = {
            "pid": int(os.getpid()),
            "process_creation_time": int(marker),
            "owner_id": owner_id,
            "generation": owner_id,
            "role": self.role,
            "scope_id": str(self.identity["scope_id"]),
            "user_hash": str(self.identity["user_hash"]),
            "session_id": int(self.identity["session_id"]),
            "created_at": _utc_now(),
        }
        _atomic_json(self.owner_path, metadata)
        self.owner_id = owner_id
        self.process_creation_time = int(marker)
        return metadata

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
            if not self._lock_stream(stream):
                stream.close()
                return False
        except (OSError, BlockingIOError):
            stream.close()
            return False
        self._file = stream
        self.acquired = True
        self.release_requested = False
        if not self.publish_owner:
            self.owner_id = ""
            self.process_creation_time = 0
            return True
        try:
            self.publish_owner_metadata()
        except Exception:
            try:
                self._unlock_stream(stream)
            finally:
                stream.close()
                self._file = None
                self.acquired = False
                self.owner_id = ""
                self.process_creation_time = 0
            raise
        return True

    def read_owner(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.owner_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            required = {"pid", "owner_id", "scope_id", "user_hash", "session_id"}
            if not required.issubset(data):
                return None
            if str(data.get("scope_id")) != str(self.identity["scope_id"]):
                return None
            pid = int(data.get("pid") or 0)
            owner_id = str(data.get("owner_id") or "")
            if pid <= 0 or len(owner_id) < 16:
                return None
            creation = int(data.get("process_creation_time") or 0)
            if creation < 0:
                return None
            data["pid"] = pid
            data["process_creation_time"] = creation
            data["session_id"] = int(data.get("session_id") or 0)
            data["role"] = str(data.get("role") or "runtime")
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            return None

    def status(self) -> dict[str, Any]:
        owner = self.read_owner() or {}
        published_self = bool(self.acquired and self.owner_id and self.process_creation_time)
        return {
            "acquired": bool(self.acquired),
            "release_requested": bool(self.release_requested),
            "owner_id": self.owner_id if published_self else str(owner.get("owner_id") or ""),
            "pid": int((os.getpid() if published_self else owner.get("pid")) or (os.getpid() if self.acquired else 0)),
            "process_creation_time": int(self.process_creation_time or owner.get("process_creation_time") or 0),
            "role": self.role if published_self else str(owner.get("role") or ""),
            "ownership": "kernel_file_lock",
            "scope": "windows_user_session" if os.name == "nt" else "user_session",
            "scope_id": str(self.identity["scope_id"]),
            "session_id": int(self.identity["session_id"]),
            "user_hash": str(self.identity["user_hash"]),
            "identity_source": str(self.identity.get("identity_source") or ""),
        }

    def defer_release(self) -> None:
        self.release_requested = True

    def release(self) -> None:
        if not self.acquired or self._file is None:
            return
        stream = self._file
        try:
            # Published metadata deliberately survives unlock as last-generation
            # metadata until the next published owner replaces it atomically.
            self._unlock_stream(stream)
        finally:
            stream.close()
            self._file = None
            self.acquired = False
            self.owner_id = ""
            self.process_creation_time = 0
            self.release_requested = False
