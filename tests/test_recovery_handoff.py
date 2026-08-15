from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from tests.test_recovery_bootstrap import RecoveryFixture
from updater import recovery_bootstrap, update_runner
from updater.recovery_handoff import (
    launch_nova_after_clear,
    perform_validated_handoff,
    spawn_handoff_helper,
)
from updater.recovery_state import load_journal, prepare_stable_recovery_runtime, transition_journal


REPO = Path(__file__).resolve().parents[1]
NOVA = REPO / "nova"
WORKER = REPO / "tests" / "_handoff_crash_worker.py"


class RecoveryHandoffTests(unittest.TestCase):
    def _validated_fixture(self, td: str):
        fx = RecoveryFixture(Path(td))
        for name in (
            "process_launch.py",
            "recovery_journal.py", "recovery_attempts.py", "recovery_files.py",
            "recovery_environment.py", "recovery_state.py", "recovery_locking.py",
            "recovery_handoff.py", "recovery_bootstrap.py",
        ):
            shutil.copy2(NOVA / "updater" / name, fx.root / "updater" / name)
        journal = fx.transaction()
        prepare_stable_recovery_runtime(fx.root)
        journal = transition_journal(
            fx.root, journal, "files_applying",
            backup_root=fx.backup_root, files_may_have_changed=True,
        )
        journal = transition_journal(fx.root, journal, "files_applied", backup_root=fx.backup_root)
        journal = transition_journal(fx.root, journal, "update_validation_in_progress", backup_root=fx.backup_root)
        journal = transition_journal(fx.root, journal, "update_validated", backup_root=fx.backup_root)
        return fx, journal

    @staticmethod
    def _clear(fx, journal, token="t" * 32, mode="post-update"):
        return transition_journal(
            fx.root,
            journal,
            "cleared",
            backup_root=fx.backup_root,
            handoff_token=token,
            handoff_mode=mode,
            handoff_from_state=journal["state"],
            handoff_expected_generation=journal["generation"],
        )

    def test_shared_handoff_spawns_helper_before_guard_release_and_clear(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            events = []

            def launcher(command, **_kwargs):
                current = load_journal(fx.root, backup_root=fx.backup_root)
                events.append(("spawn", current["state"], current["generation"], list(command)))
                return object()

            def release_guard():
                current = load_journal(fx.root, backup_root=fx.backup_root)
                events.append(("release", current["state"], current["generation"]))

            result = perform_validated_handoff(
                fx.root,
                journal,
                "post-update",
                release_guard=release_guard,
                helper_launcher=launcher,
                token_factory=lambda: "a" * 32,
            )
            self.assertTrue(result.ok, result)
            self.assertEqual(events[0][0:3], ("spawn", "update_validated", journal["generation"]))
            self.assertEqual(events[1], ("release", "update_validated", journal["generation"]))
            self.assertIn("--handoff-launch", events[0][3])
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "cleared")

    def test_active_validated_helper_never_launches_and_times_out(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            calls = []
            with mock.patch("updater.recovery_handoff.time.monotonic", side_effect=[0.0, 2.0]), \
                 mock.patch("updater.recovery_handoff.time.sleep") as sleep:
                ok, detail = launch_nova_after_clear(
                    fx.root,
                    journal["attempt_id"],
                    journal["generation"],
                    journal["state"],
                    "x" * 32,
                    "post-update",
                    timeout_seconds=1,
                    launcher=lambda *args, **kwargs: calls.append((args, kwargs)),
                )
            self.assertFalse(ok)
            self.assertEqual(detail, "handoff_clear_timeout")
            self.assertEqual(calls, [])
            sleep.assert_not_called()

    def test_helper_accepts_only_exact_attempt_generation_token_and_launches_once(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            token = "b" * 32
            cleared = self._clear(fx, journal, token=token)
            calls = []
            gui = fx.root / "external" / "Scripts" / "pythonw.exe"
            with mock.patch("updater.recovery_handoff.select_gui_python", return_value=gui), \
                 mock.patch("updater.recovery_handoff.detached_hidden_creation_flags", return_value=0x208):
                ok, detail = launch_nova_after_clear(
                    fx.root,
                    journal["attempt_id"],
                    journal["generation"],
                    journal["state"],
                    token,
                    "post-update",
                    launcher=lambda command, **kwargs: calls.append((list(command), kwargs)) or object(),
                )
            self.assertTrue(ok, detail)
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(calls[0][0][0]), gui)
            self.assertEqual(Path(calls[0][0][1]), fx.root / "app.py")
            self.assertIn("--post-update", calls[0][0])
            self.assertEqual(calls[0][1]["creationflags"], 0x208)

            wrong_calls = []
            wrong, wrong_detail = launch_nova_after_clear(
                fx.root,
                journal["attempt_id"],
                journal["generation"] + 1,
                journal["state"],
                token,
                "post-update",
                launcher=lambda *args, **kwargs: wrong_calls.append((args, kwargs)),
            )
            self.assertFalse(wrong)
            self.assertEqual(wrong_detail, "handoff_cleared_generation_mismatch")
            self.assertEqual(wrong_calls, [])
            self.assertEqual(cleared["generation"], journal["generation"] + 1)

    def test_handoff_helper_uses_selected_console_interpreter(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            console = fx.root / "external" / "Scripts" / "python.exe"
            calls = []
            with mock.patch("updater.recovery_handoff.select_console_python", return_value=console), \
                 mock.patch("updater.recovery_handoff.detached_hidden_creation_flags", return_value=0x208):
                ok, detail = spawn_handoff_helper(
                    fx.root,
                    journal["attempt_id"],
                    journal["generation"],
                    journal["state"],
                    "h" * 32,
                    "post-update",
                    launcher=lambda command, **kwargs: calls.append((list(command), kwargs)) or object(),
                )
            self.assertTrue(ok, detail)
            self.assertEqual(len(calls), 1)
            self.assertEqual(Path(calls[0][0][0]), console)
            self.assertEqual(calls[0][1]["creationflags"], 0x208)

    def test_helper_spawn_failure_keeps_validated_quarantine_and_guard(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            releases = []
            result = perform_validated_handoff(
                fx.root,
                journal,
                "post-update",
                release_guard=lambda: releases.append("released"),
                helper_launcher=mock.Mock(side_effect=OSError("spawn failed")),
            )
            self.assertFalse(result.ok)
            self.assertFalse(result.helper_spawned)
            self.assertEqual(releases, [])
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["state"], "update_validated")
            self.assertTrue(current["recovery_required"])

    def test_guard_release_failure_keeps_validated_quarantine_without_clear(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            calls = []
            result = perform_validated_handoff(
                fx.root,
                journal,
                "post-update",
                release_guard=mock.Mock(side_effect=OSError("unlock failed")),
                helper_launcher=lambda command, **kwargs: calls.append((command, kwargs)) or object(),
            )
            self.assertFalse(result.ok)
            self.assertTrue(result.helper_spawned)
            self.assertTrue(result.guard_release_attempted)
            self.assertFalse(result.guard_released)
            self.assertEqual(len(calls), 1)
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["state"], "update_validated")
            self.assertTrue(current["recovery_required"])

    def test_stale_cas_after_helper_spawn_keeps_newer_journal_and_no_authorized_launch(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            spawned = []

            def make_stale(point, _payload):
                if point == "after_handoff_spawn_before_clear":
                    current = load_journal(fx.root, backup_root=fx.backup_root)
                    transition_journal(
                        fx.root,
                        current,
                        current["state"],
                        backup_root=fx.backup_root,
                        recovery_detail="newer writer",
                    )

            result = perform_validated_handoff(
                fx.root,
                journal,
                "post-update",
                release_guard=lambda: None,
                helper_launcher=lambda command, **kwargs: spawned.append((command, kwargs)) or object(),
                crash_hook=make_stale,
                token_factory=lambda: "c" * 32,
            )
            self.assertFalse(result.ok)
            self.assertTrue(result.helper_spawned)
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["state"], "update_validated")
            self.assertGreater(current["generation"], journal["generation"])
            calls = []
            ok, _detail = launch_nova_after_clear(
                fx.root,
                journal["attempt_id"],
                journal["generation"],
                journal["state"],
                "c" * 32,
                "post-update",
                timeout_seconds=1,
                launcher=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            self.assertFalse(ok)
            self.assertEqual(calls, [])

    def test_tampered_stable_bundle_cannot_spawn_helper(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            active = fx.root / "data" / "recovery_runtime" / "active.json"
            pointer = __import__("json").loads(active.read_text(encoding="utf-8"))
            handoff = fx.root / "data" / "recovery_runtime" / "generations" / pointer["generation"] / "recovery_handoff.py"
            handoff.write_text("tampered", encoding="utf-8")
            launcher = mock.Mock()
            ok, detail = spawn_handoff_helper(
                fx.root,
                journal["attempt_id"],
                journal["generation"],
                journal["state"],
                "d" * 32,
                "post-update",
                launcher=launcher,
            )
            self.assertFalse(ok)
            self.assertIn("handoff_spawn_failed", detail)
            launcher.assert_not_called()

    def test_launch_failure_after_cleared_never_reopens_terminal_journal(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            token = "e" * 32
            cleared = self._clear(fx, journal, token=token)
            ok, detail = launch_nova_after_clear(
                fx.root,
                journal["attempt_id"],
                journal["generation"],
                journal["state"],
                token,
                "post-update",
                launcher=mock.Mock(side_effect=OSError("app spawn failed")),
            )
            self.assertFalse(ok)
            self.assertIn("nova_launch_failed", detail)
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["state"], "cleared")
            self.assertEqual(current["generation"], cleared["generation"])
            self.assertFalse(current["recovery_required"])

    def _write_launch_marker_app(self, fx):
        marker = fx.root / "data" / "handoff_launched.txt"
        (fx.root / "app.py").write_text(
            "from pathlib import Path\nimport sys\n"
            f"Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
            encoding="utf-8",
        )
        return marker

    def _run_crash_worker(self, fx, point):
        return subprocess.run(
            [sys.executable, str(WORKER), "--root", str(fx.root), "--point", point],
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(NOVA)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            shell=False,
        )

    def test_real_supervisor_death_after_handoff_spawn_keeps_quarantine_and_helper_does_not_launch(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            marker = self._write_launch_marker_app(fx)
            proc = self._run_crash_worker(fx, "after-spawn")
            self.assertEqual(proc.returncode, 92, proc.stdout)
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["attempt_id"], journal["attempt_id"])
            self.assertEqual(current["generation"], journal["generation"])
            self.assertEqual(current["state"], "update_validated")
            self.assertTrue(current["recovery_required"])
            time.sleep(2.5)
            self.assertFalse(marker.exists(), "orphaned helper launched before clear")

    def test_real_supervisor_death_after_clear_allows_independent_helper_exactly_one_launch(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            marker = self._write_launch_marker_app(fx)
            proc = self._run_crash_worker(fx, "after-clear")
            self.assertEqual(proc.returncode, 93, proc.stdout)
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "detached helper did not relaunch after cleared")
            self.assertEqual(marker.read_text(encoding="utf-8"), "--post-update")
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["attempt_id"], journal["attempt_id"])
            self.assertEqual(current["state"], "cleared")
            self.assertFalse(current["recovery_required"])

    def test_update_runner_and_recovery_bootstrap_delegate_to_same_shared_handoff(self):
        sentinel = types.SimpleNamespace(ok=True, state="cleared", detail="ok")
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            with mock.patch("updater.recovery_handoff.perform_validated_handoff", return_value=sentinel) as shared:
                result = update_runner._launch_validated_handoff(
                    fx.root, journal, "post-update", release_guard=lambda: None
                )
            self.assertIs(result, sentinel)
            shared.assert_called_once()

            with mock.patch.object(recovery_bootstrap, "perform_validated_handoff", return_value=sentinel) as recovery_shared, \
                 mock.patch.object(recovery_bootstrap, "evaluate_remaining_processes", return_value=([], [])), \
                 mock.patch.object(recovery_bootstrap, "resolve_backup", return_value=fx.backup):
                class Guard:
                    def acquire(self): return True
                    def release(self): return None
                result = recovery_bootstrap.recover_pending(
                    fx.root,
                    backup_root=fx.backup_root,
                    launch_after_success=True,
                    lock_factories={"supervisor": Guard, "runtime": Guard},
                )
            self.assertTrue(result.recovered)
            recovery_shared.assert_called_once()

    def test_update_runner_main_uses_handoff_for_validated_journal_without_direct_launch_or_double_release(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            events = []

            class Supervisor:
                def acquire(self): events.append("supervisor_acquire"); return True
                def release(self): events.append("supervisor_release")

            class Guard:
                def __init__(self): self.calls = 0
                def release(self): self.calls += 1; events.append("guard_release")

            guard = Guard()
            coordination = update_runner.ShutdownCoordination(
                True, process_terminated=True, lock_acquired=True, guard=guard
            )

            def fake_handoff(_root, exact, mode, **kwargs):
                events.append("handoff")
                self.assertEqual(exact["attempt_id"], journal["attempt_id"])
                self.assertEqual(mode, "post-update")
                kwargs["coordination"].release_guard()
                return types.SimpleNamespace(ok=True, state="cleared", detail="ok")

            no_recovery = types.SimpleNamespace(
                pending=False, recovered=False, continue_startup=True, launched=False
            )
            with mock.patch.object(update_runner, "nova_root", return_value=fx.root), \
                 mock.patch.object(update_runner, "_recovery_gate", return_value=no_recovery), \
                 mock.patch.object(update_runner, "coordinate_runtime_shutdown", return_value=coordination), \
                 mock.patch.object(update_runner, "run_update", return_value=(0, "", journal)), \
                 mock.patch.object(update_runner, "_launch_validated_handoff", side_effect=fake_handoff), \
                 mock.patch.object(update_runner, "launch_nova") as direct_launch, \
                 mock.patch.object(update_runner, "read_version", side_effect=["0.9.8", "0.9.9"]), \
                 mock.patch.object(update_runner, "write_status"):
                rc = update_runner.main([], supervisor_lock_factory=Supervisor)

            self.assertEqual(rc, 0)
            self.assertEqual(guard.calls, 1)
            direct_launch.assert_not_called()
            self.assertEqual(events.count("handoff"), 1)
            self.assertLess(events.index("guard_release"), events.index("supervisor_release"))

    def test_one_handoff_consumes_guard_once_and_second_attempt_cannot_spawn_again(self):
        with tempfile.TemporaryDirectory() as td:
            fx, journal = self._validated_fixture(td)
            releases = mock.Mock()
            launcher = mock.Mock(return_value=object())
            first = perform_validated_handoff(
                fx.root,
                journal,
                "post-update",
                release_guard=releases,
                helper_launcher=launcher,
                token_factory=lambda: "f" * 32,
            )
            self.assertTrue(first.ok)
            releases.assert_called_once()
            launcher.assert_called_once()

            second_launcher = mock.Mock(return_value=object())
            second = perform_validated_handoff(
                fx.root,
                journal,
                "post-update",
                release_guard=mock.Mock(),
                helper_launcher=second_launcher,
                token_factory=lambda: "9" * 32,
            )
            self.assertFalse(second.ok)
            second_launcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
