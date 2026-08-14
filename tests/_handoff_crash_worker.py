from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from updater import update_runner
from updater.recovery_state import load_journal


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    journal = load_journal(root)
    if journal is None:
        return 10

    def launcher(_command, **_kwargs):
        # A real child is spawned, but it is deliberately harmless and short.
        return subprocess.Popen(
            [sys.executable, "-c", "pass"],
            cwd=str(root),
            close_fds=True,
        )

    def crash(point, _payload):
        if point == "after_handoff_spawn_before_clear":
            os._exit(92)

    update_runner._launch_validated_handoff(
        root,
        journal,
        "post-update",
        launcher=launcher,
        crash_hook=crash,
        timeout_seconds=2.0,
    )
    return 11


if __name__ == "__main__":
    raise SystemExit(main())
