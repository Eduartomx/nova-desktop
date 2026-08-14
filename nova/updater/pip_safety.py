from __future__ import annotations

"""Compatibility façade for pip containment and strong POSIX identities.

Windows behavior is delegated unchanged to ``pip_safety_legacy``. On Linux the
only change is that persisted process identities use the same exact /proc
start-time representation as the stdlib recovery gate, avoiding float rounding
mismatches across serialization/restart.
"""

import os
from pathlib import Path
import sys

try:
    from . import pip_safety_legacy as _legacy
except ImportError:
    import pip_safety_legacy as _legacy

for _name in dir(_legacy):
    if _name.startswith("__"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_legacy, _name)


class PsutilProcessTree(_legacy.PsutilProcessTree):
    def identity(self, pid: int):
        target = int(pid)
        if sys.platform.startswith("linux"):
            try:
                stat = (Path("/proc") / str(target) / "stat").read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            # Fields after the executable name begin at process state (#3).
            tail = stat[stat.rfind(")") + 2:].split()
            ticks = int(tail[19])  # starttime, field 22
            btime = next(
                int(line.split()[1])
                for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
                if line.startswith("btime ")
            )
            hz = int(os.sysconf("SC_CLK_TCK"))
            creation = btime * 1_000_000 + ticks * 1_000_000 // hz
            return {
                "pid": target,
                "creation_time": int(creation),
                "role": "pip_root_or_descendant",
            }
        return super().identity(target)


def terminate_pip_tree(proc, grace_seconds: float, *, tree_api=None):
    if os.name != "nt" and tree_api is None:
        tree_api = PsutilProcessTree()
    return _legacy.terminate_pip_tree(proc, grace_seconds, tree_api=tree_api)


def launch_pip_process(command: list[str], *, cwd: str | None = None, api=None):
    return _legacy.launch_pip_process(command, cwd=cwd, api=api)


def verify_normal_completion(proc, grace_seconds: float):
    return _legacy.verify_normal_completion(proc, grace_seconds)
