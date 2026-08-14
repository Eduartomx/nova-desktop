from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from updater.recovery_bootstrap import recover_pending, updater_recovery_gate
from updater.recovery_state import (
    RECOVERY_REQUIRED_EXIT_CODE,
    RecoveryJournalError,
    create_quarantine_journal,
    evaluate_remaining_processes,
    journal_path,
    load_journal,
    resolve_backup,
    restore_backup_idempotent,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _ExclusiveState:
    def __init__(self):
        self.guard = threading.Lock()
        self.held = False


class _TestLock:
    def __init__(self, state: _ExclusiveState, name: str):
        self.state = state
        self.name = name
        self.acquired = False

    def acquire(self):
        with self.state.guard:
            if self.state.held:
                return False
            self.state.held = True
            self.acquired = True
            return True

    def release(self):
        with self.state.guard:
            if self.acquired:
                self.state.held = False
                self.acquired = False


class RecoveryFixture:
    def __init__(self, base: Path):
        self.root = base / "nova"
        self.backup_root = self.root / "data" / "updater_backups"
        self.backup = self.backup_root / "attempt-1"
        (self.root / "updater").mkdir(parents=True)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.backup.mkdir(parents=True)
        (self.root / "app.py").write_text("VALUE = 'restored-app'\n", encoding="utf-8")
        (self.root / "updater" / "nova_updater.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "updater" / "update_runner.py").write_text("VALUE = 2\n", encoding="utf-8")
        (self.root / "modified.txt").write_text("new-modified", encoding="utf-8")
        (self.root / "created.txt").write_text("new-created", encoding="utf-8")
        (self.root / "updater" / "managed_files.json").write_text('{"files":["created.txt"]}', encoding="utf-8")

        files = self.backup / "files"
        files.mkdir()
        (files / "modified.txt").write_text("old-modified", encoding="utf-8")
        (files / "deleted.txt").write_text("old-deleted", encoding="utf-8")
        control = self.backup / "control"
        control.mkdir()
        (control / "managed_files.json").write_text('{"files":["modified.txt","deleted.txt"]}', encoding="utf-8")
        manifest = {
            "schema": 2,
            "from": "0.9.8",
            "to": "0.9.9",
            "modified_existing": ["modified.txt"],
            "deleted_existing": ["deleted.txt"],
            "created_new": ["created.txt"],
            "unchanged": ["app.py"],
            "managed_files": {
                "path": "updater/managed_files.json",
                "existed": True,
                "backup": "control/managed_files.json",
            },
        }
        (self.backup / "backup.json").write_text(json.dumps(manifest), encoding="utf-8")

    def quarantine(self, remaining=None, **kwargs):
        return create_quarantine_journal(
            self.root,
            self.backup,
            backup_root=self.backup_root,
            remaining_processes=list(remaining or []),
            **kwargs,
        )


class StrongIdentityTests(unittest.TestCase):
    def test_matching_pid_and_creation_time_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            identity = {"pid": 1234, "creation_time": 555000, "role": "pip_root_or_descendant"}
            journal = fx.quarantine([identity])
            blocking, errors = evaluate_remaining_processes(journal, inspector=lambda row: ("alive", ""))
            self.assertEqual(blocking, [identity])
            self.assertEqual(errors, [])

    def test_same_pid_different_creation_time_is_reused_and_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.quarantine([{"pid": 1234, "creation_time": 555000, "role": "pip_root_or_descendant"}])
            blocking, errors = evaluate_remaining_processes(journal, inspector=lambda row: ("reused", ""))
            self.assertEqual(blocking, [])
            self.assertEqual(errors, [])

    def test_identity_inspection_error_is_conservative(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.quarantine([{"pid": 1234, "creation_time": 555000, "role": "pip_root_or_descendant"}])
            blocking, errors = evaluate_remaining_processes(journal, inspector=lambda row: ("unknown", "access_denied"))
            self.assertEqual(len(blocking), 1)
            self.assertIn("access_denied", errors)

    def test_float_creation_time_is_rejected_to_avoid_precision_loss(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            with self.assertRaises(RecoveryJournalError):
                fx.quarantine([{"pid": 1234, "creation_time": 555000.25, "role": "pip_root_or_descendant"}])


class StartupGateTests(unittest.TestCase):
    def test_app_gate_runs_before_claim_instance_and_normal_stack(self):
        import app
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(app, "_startup_recovery_gate", return_value=(False, RECOVERY_REQUIRED_EXIT_CODE)) as gate, \
             mock.patch.object(app, "_claim_instance", side_effect=AssertionError("instance claimed before recovery gate")) as claim:
            rc = app.main([])
        self.assertEqual(rc, RECOVERY_REQUIRED_EXIT_CODE)
        gate.assert_called_once()
        claim.assert_not_called()

    def test_app_source_has_no_top_level_tk_or_assistant_import_before_gate(self):
        source = (Path(__file__).resolve().parents[1] / "nova" / "app.py").read_text(encoding="utf-8")
        main_body = source[source.index("def main"):]
        self.assertLess(main_body.index("_startup_recovery_gate"), main_body.index("_claim_instance"))
        prefix = source[:source.index("def _claim_instance")]
        self.assertNotIn("import tkinter", prefix)
        self.assertNotIn("from assistant", prefix)

    def test_live_process_quarantine_returns_dedicated_code_without_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine([{"pid": 1234, "creation_time": 555000, "role": "pip_root_or_descendant"}])
            restore = mock.Mock()
            result = recover_pending(
                fx.root, backup_root=fx.backup_root,
                inspector=lambda row: ("alive", ""), restore_func=restore,
                launch_after_success=False,
            )
            self.assertTrue(result.pending)
            self.assertEqual(result.exit_code, RECOVERY_REQUIRED_EXIT_CODE)
            restore.assert_not_called()


class RecoveryFlowTests(unittest.TestCase):
    def test_recovery_restores_exact_files_managed_state_validates_clears_and_launches_once(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            expected_modified = _sha(fx.backup / "files" / "modified.txt")
            expected_deleted = _sha(fx.backup / "files" / "deleted.txt")
            expected_managed = _sha(fx.backup / "control" / "managed_files.json")
            fx.quarantine()
            launches = []
            result = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                inspector=lambda row: ("gone", ""),
                launcher=lambda command, **kwargs: launches.append((command, kwargs)),
                launch_after_success=True,
            )
            self.assertTrue(result.recovered)
            self.assertTrue(result.launched)
            self.assertFalse(result.pending)
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0][0][-1], "--post-recovery")
            self.assertEqual(_sha(fx.root / "modified.txt"), expected_modified)
            self.assertEqual(_sha(fx.root / "deleted.txt"), expected_deleted)
            self.assertFalse((fx.root / "created.txt").exists())
            self.assertEqual(_sha(fx.root / "updater" / "managed_files.json"), expected_managed)
            journal = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(journal["state"], "cleared")
            self.assertFalse(journal["recovery_required"])
            # A second invocation is idempotent: no second restore/launch.
            second_launch = mock.Mock()
            second = recover_pending(
                fx.root, backup_root=fx.backup_root,
                launcher=second_launch, launch_after_success=True,
            )
            self.assertTrue(second.continue_startup)
            second_launch.assert_not_called()

    def test_crash_mid_rollback_is_resumed_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine()
            calls = []

            def crash_restore(root, backup):
                calls.append("partial")
                (Path(root) / "modified.txt").write_text("old-modified", encoding="utf-8")
                raise RuntimeError("simulated crash")

            first = recover_pending(
                fx.root, backup_root=fx.backup_root,
                restore_func=crash_restore, validator=lambda root: (True, "ok"),
                launch_after_success=False,
            )
            self.assertTrue(first.pending)
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "rollback_in_progress")
            second = recover_pending(
                fx.root, backup_root=fx.backup_root,
                validator=lambda root: (True, "ok"), launch_after_success=False,
            )
            self.assertTrue(second.recovered)
            self.assertTrue(second.continue_startup)
            self.assertEqual((fx.root / "modified.txt").read_text(encoding="utf-8"), "old-modified")
            self.assertEqual((fx.root / "deleted.txt").read_text(encoding="utf-8"), "old-deleted")
            self.assertFalse((fx.root / "created.txt").exists())
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "cleared")

    def test_rollback_failure_keeps_quarantine_and_backup(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine()
            result = recover_pending(
                fx.root, backup_root=fx.backup_root,
                restore_func=lambda *_: (_ for _ in ()).throw(OSError("restore blocked")),
                launch_after_success=False,
            )
            self.assertTrue(result.pending)
            journal = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(journal["state"], "rollback_in_progress")
            self.assertTrue(journal["recovery_required"])
            self.assertTrue(fx.backup.exists())

    def test_validation_failure_keeps_quarantine_and_backup(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine()
            result = recover_pending(
                fx.root, backup_root=fx.backup_root,
                validator=lambda root: (False, "validation failed"),
                launch_after_success=False,
            )
            self.assertTrue(result.pending)
            journal = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(journal["state"], "validation_in_progress")
            self.assertTrue(journal["recovery_required"])
            self.assertTrue(fx.backup.exists())

    def test_two_recovery_supervisors_only_one_executes_restore(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine()
            supervisor_state = _ExclusiveState()
            runtime_state = _ExclusiveState()
            entered = threading.Event()
            release = threading.Event()
            restore_calls = []

            def restore(root, backup):
                restore_calls.append("restore")
                entered.set()
                self.assertTrue(release.wait(5), "test recovery owner was never released")
                restore_backup_idempotent(root, backup)

            factories = {
                "supervisor": lambda: _TestLock(supervisor_state, "supervisor"),
                "runtime": lambda: _TestLock(runtime_state, "runtime"),
            }
            first_result = []
            thread = threading.Thread(
                target=lambda: first_result.append(recover_pending(
                    fx.root, backup_root=fx.backup_root, restore_func=restore,
                    validator=lambda root: (True, "ok"), launch_after_success=False,
                    lock_factories=factories,
                )),
                daemon=True,
            )
            thread.start()
            self.assertTrue(entered.wait(5), "first recovery never entered rollback")
            second = recover_pending(
                fx.root, backup_root=fx.backup_root,
                validator=lambda root: (True, "ok"), launch_after_success=False,
                lock_factories=factories,
            )
            self.assertTrue(second.pending)
            self.assertEqual(second.exit_code, RECOVERY_REQUIRED_EXIT_CODE)
            self.assertIn("supervisor", second.detail)
            self.assertEqual(restore_calls, ["restore"])
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(first_result), 1)
            self.assertTrue(first_result[0].recovered)


class RecoveryJournalSafetyTests(unittest.TestCase):
    def test_truncated_journal_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            path = journal_path(fx.root)
            path.write_text('{"schema_version":1,"attempt_id":', encoding="utf-8")
            result = recover_pending(fx.root, backup_root=fx.backup_root, launch_after_success=False)
            self.assertTrue(result.pending)
            self.assertEqual(result.exit_code, RECOVERY_REQUIRED_EXIT_CODE)
            self.assertEqual(result.state, "corrupt")

    def test_unknown_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            payload = {
                "schema_version": 99, "attempt_id": "a" * 32, "generation": 1,
                "state": "waiting_for_processes", "recovery_required": True,
                "backup_path": "attempt-1", "remaining_processes": [], "errors": [],
            }
            journal_path(fx.root).write_text(json.dumps(payload), encoding="utf-8")
            result = recover_pending(fx.root, backup_root=fx.backup_root, launch_after_success=False)
            self.assertTrue(result.pending)
            self.assertEqual(result.exit_code, RECOVERY_REQUIRED_EXIT_CODE)

    def test_backup_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            payload = {
                "schema_version": 1, "attempt_id": "a" * 32, "generation": 1,
                "state": "waiting_for_processes", "recovery_required": True,
                "backup_path": "../outside", "created_at": "x", "updated_at": "x",
                "dependencies_may_have_changed": True, "files_rollback_attempted": False,
                "files_rollback_ok": False, "remaining_processes": [], "errors": [],
            }
            journal_path(fx.root).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RecoveryJournalError):
                load_journal(fx.root, backup_root=fx.backup_root)

    def test_symlink_escape_from_backup_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fx = RecoveryFixture(base)
            outside = base / "outside"
            outside.mkdir()
            (outside / "backup.json").write_text("{}", encoding="utf-8")
            link = fx.backup_root / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable")
            payload = {
                "schema_version": 1, "attempt_id": "a" * 32, "generation": 1,
                "state": "waiting_for_processes", "recovery_required": True,
                "backup_path": "escape", "created_at": "x", "updated_at": "x",
                "dependencies_may_have_changed": True, "files_rollback_attempted": False,
                "files_rollback_ok": False, "remaining_processes": [], "errors": [],
            }
            journal_path(fx.root).write_text(json.dumps(payload), encoding="utf-8")
            journal = load_journal(fx.root, backup_root=fx.backup_root)
            with self.assertRaises(RecoveryJournalError):
                resolve_backup(fx.root, journal, backup_root=fx.backup_root)

    def test_updater_gate_preserves_existing_journal_and_backup_when_process_still_alive(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine([{"pid": 44, "creation_time": 900, "role": "pip_root_or_descendant"}])
            before = journal_path(fx.root).read_bytes()
            result = updater_recovery_gate(
                fx.root, backup_root=fx.backup_root,
                inspector=lambda row: ("alive", ""),
            )
            self.assertTrue(result.pending)
            self.assertEqual(result.exit_code, RECOVERY_REQUIRED_EXIT_CODE)
            self.assertEqual(journal_path(fx.root).read_bytes(), before)
            self.assertTrue(fx.backup.exists())


if __name__ == "__main__":
    unittest.main()
