from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import types

from updater import resident_update_engine as engine
from updater import pip_safety


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--point", required=True)
    parser.add_argument("--pid-file", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    stage = Path(args.stage).resolve()
    engine.base.ROOT = root
    engine.base.MANAGED_PATH = root / "updater" / "managed_files.json"
    engine.base.CONFIG_PATH = root / "updater" / "update_config.json"

    original_install = engine._install_requirements

    def successful_install(*_a, on_started=None, **_kw):
        if on_started is not None:
            on_started(types.SimpleNamespace(pid=0))
        return True

    engine._install_requirements = successful_install
    if args.point in {
        "after_failure_before_rollback", "mid_rollback",
        "after_restore_before_validation", "after_validation_before_clear",
    }:
        def fail_install(*_a, **_kw):
            raise engine.DependencyInstallError(
                "simulated dependency setup failure", dependency_started=False
            )
        engine._install_requirements = fail_install
    elif args.point == "dependencies_running":
        def contained_harmless_install(*_a, on_started=None, **_kw):
            proc = pip_safety.launch_pip_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=str(root),
            )
            if on_started is not None:
                on_started(proc)
            proc.wait(timeout=30)
            return True
        engine._install_requirements = contained_harmless_install

    def crash(point, context):
        if point != args.point:
            return
        if args.pid_file and context.get("pid"):
            Path(args.pid_file).write_text(str(int(context["pid"])), encoding="utf-8")
        os._exit(91)

    files = ["a.txt", "b.txt", "c.txt", "requirements.txt"]
    previous = set(files)
    try:
        engine.execute_transaction(
            stage, files, previous, "v-test", "old", "new",
            backup_root=root / "data" / "updater_backups",
            pip_timeout_seconds=30,
            crash_hook=crash,
        )
    finally:
        engine._install_requirements = original_install
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
