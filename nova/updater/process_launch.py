from __future__ import annotations

"""Stdlib-only interpreter selection and Windows launch profiles.

The updater must preserve the environment that owns Nova.  In particular, a
``pythonw.exe`` process may expose a base interpreter through implementation
details; those details are deliberately ignored here.  Selection is based only
on the managed environment and on siblings of the current executable.
"""

import os
from pathlib import Path
import sys


CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def _current_executable(current_executable: str | os.PathLike[str] | None) -> Path:
    return Path(current_executable) if current_executable is not None else Path(sys.executable)


def select_console_python(
    root: str | os.PathLike[str],
    current_executable: str | os.PathLike[str] | None = None,
) -> Path:
    """Select a console interpreter without escaping to a base interpreter."""
    managed = Path(root) / ".venv" / "Scripts" / "python.exe"
    if managed.is_file():
        return managed

    current = _current_executable(current_executable)
    if current.name.casefold() == "pythonw.exe":
        sibling = current.with_name("python.exe")
        if sibling.is_file():
            return sibling
    return current


def select_gui_python(
    root: str | os.PathLike[str],
    current_executable: str | os.PathLike[str] | None = None,
) -> Path:
    """Select the environment-preserving GUI interpreter when one exists."""
    managed = Path(root) / ".venv" / "Scripts" / "pythonw.exe"
    if managed.is_file():
        return managed

    current = _current_executable(current_executable)
    if current.name.casefold() == "pythonw.exe":
        return current

    sibling = current.with_name("pythonw.exe")
    if sibling.is_file():
        return sibling
    return select_console_python(root, current)


def hidden_supervisor_creation_flags(platform_name: str | None = None) -> int:
    """Windows profile for an attached lifetime with no visible console."""
    if (platform_name or os.name) != "nt":
        return 0
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def detached_hidden_creation_flags(platform_name: str | None = None) -> int:
    """Windows profile for a handoff/app that outlives its launcher, hidden."""
    if (platform_name or os.name) != "nt":
        return 0
    return DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
