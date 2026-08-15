from __future__ import annotations

"""Compatibility surface for pip containment and strong POSIX identities.

Production Windows containment remains the native Job Object implementation in
``pip_safety_legacy``.  The import-time guard below exists solely to make the
historical updater implementation non-executable as a standalone script: its
normal package import path is unaffected.
"""

import os
from pathlib import Path
import sys

if __name__ == "pip_safety" and Path(sys.argv[0]).name.casefold() == "nova_updater_legacy.py":
    print("[ERROR] nova_updater_legacy.py es import-only; usa updater/update_runner.py.")
    raise SystemExit(4)

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
            tail = stat[stat.rfind(")") + 2:].split()
            ticks = int(tail[19])
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
