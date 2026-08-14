from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import psutil

from updater.recovery_bootstrap import recover_pending
from updater.recovery_state import load_journal


REPO = Path(__file__).resolve().parents[1]
NOVA = REPO / "nova"
WORKER = REPO / "tests" / "_update_crash_worker.py"


class UpdateCrashRecoveryTests(unittest.TestCase):
    def _fixture(self, td: str):
        root = Path(td) / "nova"
        stage = Path(td) / "stage"
        (root / "updater").mkdir(parents=True)
        (root / "data" / "updater_backups").mkdir(parents=True)
        stage.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (root / name).write_text("old-" + name, encoding="utf-8")
            (stage / name).write_text("new-" + name, encoding="utf-8")
        (root / "requirements.txt").write_text("psutil\n", encoding="utf-8")
        (stage / "requirements.txt").write_text("psutil\nrequests\n", encoding="utf-8")
        managed = {"tag": "old", "files": ["a.txt", "b.txt", "c.txt", "requirements.txt"]}
        (root / "updater" / "managed_files.json").write_text(json.dumps(managed), encoding="utf-8")
        for module in (
            "recovery_journal.py", "recovery_attempts.py", "recovery_files.py",
            "recovery_environment.py", "recovery_state.py", "recovery_locking.py",
            "recovery_bootstrap.py",
        ):
            shutil.copy2(NOVA / "updater" / module, root / "updater" / module)
        (root / "app.py").write_text("VALUE=1\n", encoding="utf-8")
        (root / "updater" / "nova_updater.py").write_text("VALUE=1\n", encoding="utf-8")
        (root / "updater" / "update_runner.py").write_text("VALUE=1\n", encoding="utf-8")
        old = {name: (root / name).read_bytes() for name in ("a.txt", "b.txt", "c.txt", "requirements.txt")}
        old["managed"] = (root / "updater" / "managed_files.json").read_bytes()
        return root, stage, old

    def _crash(self, root: Path, stage: Path, point: str, pid_file: Path | None = None):
        command = [sys.executable, str(WORKER), "--root", str(root), "--stage", str(stage), "--point", point]
        if pid_file is not None:
            command += ["--pid-file", str(pid_file)]
        proc = subprocess.run(
            command, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=40, shell=False,
            env={**os.environ, "PYTHONPATH": str(NOVA)},
        )
        self.assertEqual(proc.returncode, 91, f"{point}: {proc.stdout}")

    def _assert_durable_and_recover(self, root: Path, old: dict[str, bytes], *, expect_rollback: bool = True):
        journal = load_journal(root)
        self.assertIsNotNone(journal)
        self.assertTrue(journal["recovery_required"])
        backup = root / "data" / "updater_backups" / journal["backup_path"]
        self.assertTrue((backup / "backup.json").is_file())
        result = recover_pending(
            root,
            validator=lambda *_args: (True, "fixture validated"),
            launch_after_success=False,
        )
        self.assertTrue(result.recovered, result)
        self.assertEqual(load_journal(root)["state"], "cleared")
        if expect_rollback:
            for name in ("a.txt", "b.txt", "c.txt", "requirements.txt"):
                self.assertEqual((root / name).read_bytes(), old[name], name)
            self.assertEqual((root / "updater" / "managed_files.json").read_bytes(), old["managed"])
        else:
            for name in ("a.txt", "b.txt", "c.txt"):
                self.assertEqual((root / name).read_text(encoding="utf-8"), "new-" + name)
            self.assertEqual((root / "requirements.txt").read_text(encoding="utf-8"), "psutil\nrequests\n")
        self.assertTrue(backup.exists(), "backup must survive recovery")

    def test_real_crashes_across_transaction_boundaries_resume_idempotently(self):
        points = [
            "after_journal_before_files",
            "after_first_file",
            "mid_apply",
            "after_files_applied",
            "after_dependencies_may_change_before_pip",
            "after_failure_before_rollback",
            "mid_rollback",
            "after_restore_before_validation",
            "after_validation_before_clear",
            "after_update_validated_before_clear",
        ]
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as td:
                root, stage, old = self._fixture(td)
                self._crash(root, stage, point)
                self._assert_durable_and_recover(
                    root, old, expect_rollback=(point != "after_update_validated_before_clear")
                )
                second = recover_pending(root, launch_after_success=False)
                self.assertTrue(second.continue_startup)
                self.assertFalse(second.recovered)

    @unittest.skipUnless(sys.platform == "win32", "requires real Windows Job Object")
    def test_killing_updater_while_job_contains_dependency_leaves_no_child(self):
        with tempfile.TemporaryDirectory() as td:
            root, stage, old = self._fixture(td)
            pid_file = Path(td) / "pip.pid"
            self._crash(root, stage, "dependencies_running", pid_file)
            self.assertTrue(pid_file.is_file())
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            try:
                child = psutil.Process(child_pid)
                child.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            self.assertFalse(psutil.pid_exists(child_pid), "Job Object child survived updater death")
            self._assert_durable_and_recover(root, old)


if __name__ == "__main__":
    unittest.main()
