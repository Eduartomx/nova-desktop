from __future__ import annotations

"""Kernel-backed single-instance ownership for one Nova runtime per user session.

The lock itself is authoritative. ``owner.json`` is only metadata used to target
local commands and to let the updater capture the owning process before asking
it to terminate. Metadata never contains a raw Windows SID.
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


def _windows_identity() -> tuple[str, int]:
    """Return (hashed user SID, process Session ID) with typed Win32 calls."""
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
    pid = kernel32.GetCurrentProcessId()
    if not kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
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
        # TOKEN_USER starts with SID_AND_ATTRIBUTES; the first field is PSID.
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
    except Exception:
        # Safe deterministic fallback: still scoped to the current profile/session.
        user_raw = "|".join((os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""), str(Path.home())))
        session_raw = os.environ.get("SESSIONNAME") or os.environ.get("XDG_SESSION_ID") or "default"
        user_hash = _hash_text(user_raw)
        session_id = int(_hash_text(session_raw, 8), 16)
    scope_id = _hash_text(f"{user_hash}|{session_id}")
    return {"user_hash": user_hash, "session_id": int(session_id), "scope_id": scope_id}


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
    def __init__(self, *, path: Path | None = None, owner_path: Path | None = None, locker=None):
        paths = runtime_paths()
        self.path = Path(path) if path is not None else paths.lock
        self.owner_path = Path(owner_path) if owner_path is not None else (paths.owner if path is None else self.path.with_name("owner.json"))
        self._locker = locker
        self._file = None
        self.acquired = False
        self.owner_id = ""
        self.identity = runtime_identity()

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
        self.owner_id = uuid.uuid4().hex
        metadata = {
            "pid": int(os.getpid()),
            "owner_id": self.owner_id,
            "generation": self.owner_id,
            "scope_id": str(self.identity["scope_id"]),
            "user_hash": str(self.identity["user_hash"]),
            "session_id": int(self.identity["session_id"]),
            "created_at": _utc_now(),
        }
        try:
            _atomic_json(self.owner_path, metadata)
        except Exception:
            try:
                self._unlock_stream(stream)
            finally:
                stream.close()
                self._file = None
                self.acquired = False
                self.owner_id = ""
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
            data["pid"] = pid
            data["session_id"] = int(data.get("session_id") or 0)
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeError):
            return None

    def status(self) -> dict[str, Any]:
        owner = self.read_owner() or {}
        return {
            "acquired": bool(self.acquired),
            "owner_id": self.owner_id if self.acquired else str(owner.get("owner_id") or ""),
            "pid": int(owner.get("pid") or (os.getpid() if self.acquired else 0)),
            "ownership": "kernel_file_lock",
            "scope": "windows_user_session" if os.name == "nt" else "user_session",
            "scope_id": str(self.identity["scope_id"]),
            "session_id": int(self.identity["session_id"]),
            "user_hash": str(self.identity["user_hash"]),
        }

    def release(self) -> None:
        if not self.acquired or self._file is None:
            return
        stream = self._file
        owner_id = self.owner_id
        try:
            current = self.read_owner()
            if current and str(current.get("owner_id")) == owner_id:
                try:
                    self.owner_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._unlock_stream(stream)
        finally:
            stream.close()
            self._file = None
            self.acquired = False
            self.owner_id = ""
