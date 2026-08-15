from __future__ import annotations

import argparse
import os
from pathlib import Path

from updater import update_runner
from updater.recovery_state import load_journal


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--point", choices=("after-spawn", "after-clear"), default="after-spawn")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    journal = load_journal(root)
    if journal is None:
        return 10

    wanted = "after_handoff_spawn_before_clear" if args.point == "after-spawn" else "after_handoff_clear"

    def crash(point, _payload):
        if point == wanted:
            os._exit(92 if args.point == "after-spawn" else 93)

    update_runner._launch_validated_handoff(
        root,
        journal,
        "post-update",
        crash_hook=crash,
        timeout_seconds=2.0,
    )
    return 11


if __name__ == "__main__":
    raise SystemExit(main())
