from __future__ import annotations

"""Exclusive update-supervisor mutex scoped to the current user/session.

The kernel-backed file lock is authoritative.  No PID or metadata file is used
as a substitute for ownership; metadata from the runtime lock remains entirely
separate from this mutex.
"""

from pathlib import Path
from typing import Any

from .instance_lock import InstanceLock, runtime_paths

SUPERVISOR_LOCK_FILENAME = "update_supervisor.lock"


def supervisor_mutex_path() -> Path:
    return runtime_paths().directory / SUPERVISOR_LOCK_FILENAME


def create_supervisor_mutex(*, path: Path | None = None, locker=None):
    lock_path = Path(path) if path is not None else supervisor_mutex_path()
    return InstanceLock(
        path=lock_path,
        owner_path=lock_path.with_name("update_supervisor.owner-unused.json"),
        locker=locker,
        publish_owner=False,
        role="update_supervisor",
    )


def supervisor_status(*, lock_factory=None) -> dict[str, Any]:
    """Probe the kernel lock without persisting ownership metadata."""
    factory = lock_factory or create_supervisor_mutex
    lock = None
    try:
        lock = factory()
        acquired = bool(lock.acquire())
        if acquired:
            try:
                lock.release()
            finally:
                lock = None
            return {
                "active": False,
                "ownership": "kernel_file_lock",
                "scope": "user_session",
                "error": "",
            }
        return {
            "active": True,
            "ownership": "kernel_file_lock",
            "scope": "user_session",
            "error": "",
        }
    except Exception as exc:
        return {
            "active": None,
            "ownership": "kernel_file_lock",
            "scope": "user_session",
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
    finally:
        if lock is not None and getattr(lock, "acquired", False):
            try:
                lock.release()
            except Exception:
                pass
