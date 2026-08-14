from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.test_recovery_bootstrap import RecoveryFixture
from updater.recovery_bootstrap import recover_pending
from updater.recovery_handoff import launch_nova_after_clear, spawn_handoff_helper, wait_for_cleared_attempt
from updater.recovery_state import load_journal, prepare_stable_recovery_runtime, transition_journal


REPO = Path(__file__).resolve().parents[1]
NOVA = REPO / "nova"
WORKER = REPO / "tests" / "_handoff_crash_worker.py"


class RecoveryHandoffTests(unittest.TestCase):
    def _validated_fixture(self, td: str):
        fx = RecoveryFixture(Path(td))
        for name in (
            "recovery_journal.py", "recovery_attempts.py", "recovery_files.py",
            "recovery_environment.py", "recovery_state.py", "recovery_locking.py",
            "recovery_handoff.py", "recovery_bootstrap.py",
        ):
            shutil.copy2(NOVA / "updater" / name, fx.root / "updater" / name)
        journal = fx.transaction()
        journal = transition_journal(
            fx.root, journal, "files_applying",
            backup_root=fx.backup_root, files_may_have_changed=True,
        )
        journal = transition_journal(fx.root, journal, "files_applied", backup_root=fx.backup_root)
        journal = transition_journal(fx.root, journal, "update_validation_in_progress", backup_root=fx.backup_root)
        journal = transition_journal(fx.root, journal, "update_validated", backup_root=fx.backup_root)
        prepare_stable_recovery_runtime(fx.root)
        return fx, journal

    def test_stable_helper_is_spawned_before_clear_for_exact_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            calls = []
            def launcher(command, **kwargs):
                calls.append((list(command), dict(kwargs)))
                return object()
            ok, detail = spawn_handoff_helper(
                fx.root, journal["attempt_id"], "post-update", launcher=launcher
            )
            self.assertTrue(ok, detail)
            self.assertEqual(len(calls), 1)
            command = calls[0][0]
            self.assertIn("--handoff-launch", command)
            self.assertIn("--attempt-id", command)
            self.assertIn(journal["attempt_id"], command)
            self.assertIn("post-update", command)
            self.assertIn("recovery_runtime", command[1])

    def test_waiter_accepts_only_same_attempt_after_clear(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            cleared = transition_journal(fx.root, journal, "cleared", backup_root=fx.backup_root)
            ok, detail = wait_for_cleared_attempt(fx.root, cleared["attempt_id"], timeout_seconds=1)
            self.assertTrue(ok, detail)
            wrong, wrong_detail = wait_for_cleared_attempt(fx.root, "f" * 32, timeout_seconds=1)
            self.assertFalse(wrong)
            self.assertEqual(wrong_detail, "handoff_attempt_changed")

    def test_active_validated_attempt_times_out_without_launch(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            with mock.patch("updater.recovery_handoff.time.monotonic", side_effect=[0.0, 2.0]), \
                 mock.patch("updater.recovery_handoff.time.sleep") as sleep:
                ok, detail = wait_for_cleared_attempt(fx.root, journal["attempt_id"], timeout_seconds=1)
            self.assertFalse(ok)
            self.assertEqual(detail, "handoff_clear_timeout")
            sleep.assert_not_called()

    def test_helper_launches_nova_only_after_cleared(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            cleared = transition_journal(fx.root, journal, "cleared", backup_root=fx.backup_root)
            calls = []
            ok, detail = launch_nova_after_clear(
                fx.root,
                cleared["attempt_id"],
                "post-update",
                launcher=lambda command, **kwargs: calls.append((list(command), kwargs)) or object(),
            )
            self.assertTrue(ok, detail)
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(calls[0][0][1]), fx.root / "app.py")
            self.assertIn("--post-update", calls[0][0])

    def test_real_supervisor_death_after_handoff_spawn_keeps_quarantine(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            proc = subprocess.run(
                [sys.executable, str(WORKER), "--root", str(fx.root)],
                cwd=str(REPO),
                env={**os.environ, "PYTHONPATH": str(NOVA)},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=20,
                shell=False,
            )
            self.assertEqual(proc.returncode, 92, proc.stdout)
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["attempt_id"], journal["attempt_id"])
            self.assertEqual(current["state"], "update_validated")
            self.assertTrue(current["recovery_required"])
            resumed = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                launch_after_success=False,
            )
            self.assertTrue(resumed.recovered, resumed)
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "cleared")


if __name__ == "__main__":
    unittest.main()
