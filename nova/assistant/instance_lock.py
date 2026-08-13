from __future__ import annotations

"""Kernel-backed per-user instance ownership for Nova on Windows."""

import hashlib
import os
from pathlib import Path


def _scope_id() -> str:
    raw = "|".join((os.environ.get("USERNAME", ""), os.environ.get("USERDOMAIN", ""), os.environ.get("SESSIONNAME", "")))
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def runtime_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".nova"))
    return base / "Nova" / "runtime"


class InstanceLock:
    def __init__(self, *, path: Path | None = None, locker=None):
        self.path = Path(path) if path is not None else runtime_directory() / f"instance-{_scope_id()}.lock"
        self._locker = locker
        self._file = None
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if self._locker is not None:
                ok = bool(self._locker.acquire(stream))
                if not ok:
                    stream.close()
                    return False
            elif os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                # Production app is Windows-only; tests inject a locker.
                stream.close()
                return False
        except OSError:
            stream.close()
            return False
        self._file = stream
        self.acquired = True
        return True

    def status(self) -> dict:
        return {
            "acquired": bool(self.acquired),
            "ownership": "windows_kernel_file_lock",
            "scope": "current_user_session",
        }

    def release(self) -> None:
        if not self.acquired or self._file is None:
            return
        try:
            self._file.seek(0)
            if self._locker is not None:
                self._locker.release(self._file)
            elif os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None
            self.acquired = False
