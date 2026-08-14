from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from updater.recovery_bootstrap import recover_pending, updater_recovery_gate
from updater.recovery_state import (
    RECOVERY_REQUIRED_EXIT_CODE,
    RecoveryJournalError,
    StaleJournalWriterError,
    create_quarantine_journal,
    create_transaction_journal,
    evaluate_remaining_processes,
    journal_path,
    load_journal,
    resolve_backup,
    transition_journal,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecoveryFixture:
    def __init__(self, base: Path):
        self.root = base / "nova"
        self.backup_root = self.root / "data" / "updater_backups"
        self.backup = self.backup_root / "attempt-1"
        (self.root / "updater").mkdir(parents=True)
        self.backup.mkdir(parents=True)
        (self.root / "app.py").write_text("VALUE=1\n", encoding="utf-8")
        (self.root / "updater" / "nova_updater.py").write_text("VALUE=1\n", encoding="utf-8")
        (self.root / "updater" / "update_runner.py").write_text("VALUE=1\n", encoding="utf-8")
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
        (self.backup / "backup.json").write_text(json.dumps({
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
        }), encoding="utf-8")

    def quarantine(self, remaining=None, **kwargs):
        return create_quarantine_journal(
            self.root,
            self.backup,
            backup_root=self.backup_root,
            remaining_processes=list(remaining or []),
            **kwargs,
        )

    def transaction(self):
        return create_transaction_journal(
            self.root,
            self.backup,
            backup_root=self.backup_root,
        )


class StrongIdentityTests(unittest.TestCase):
    def test_pid_and_creation_time_match_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            identity = {"pid": 1234, "creation_time": 555000, "role": "pip_root_or_descendant"}
            journal = fx.quarantine([identity])
            blocking, errors = evaluate_remaining_processes(journal, inspector=lambda row: ("alive", ""))
            self.assertEqual(blocking, [identity])
            self.assertEqual(errors, [])

    def test_reused_pid_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.quarantine([{"pid": 1234, "creation_time": 555000, "role": "pip_root_or_descendant"}])
            blocking, errors = evaluate_remaining_processes(journal, inspector=lambda row: ("reused", ""))
            self.assertEqual(blocking, [])
            self.assertEqual(errors, [])

    def test_inspection_error_is_conservative(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.quarantine([{"pid": 1234, "creation_time": 555000, "role": "pip_root_or_descendant"}])
            blocking, errors = evaluate_remaining_processes(journal, inspector=lambda row: ("unknown", "access_denied"))
            self.assertEqual(len(blocking), 1)
            self.assertIn("access_denied", errors)

    def test_lossy_creation_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            with self.assertRaises(RecoveryJournalError):
                fx.quarantine([{"pid": 1, "creation_time": 1.5, "role": "pip_root_or_descendant"}])


class RecoveryStateMachineTests(unittest.TestCase):
    def test_schema_two_starts_transaction_prepared_before_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            self.assertEqual(journal["schema_version"], 2)
            self.assertEqual(journal["state"], "transaction_prepared")
            self.assertTrue(journal["recovery_required"])
            self.assertFalse(journal["files_may_have_changed"])

    def test_illegal_transition_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            with self.assertRaises(RecoveryJournalError):
                transition_journal(
                    fx.root,
                    journal,
                    "dependencies_running",
                    backup_root=fx.backup_root,
                )

    def test_stale_generation_cannot_overwrite_newer_state(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            stale = fx.transaction()
            current = transition_journal(
                fx.root,
                stale,
                "files_applying",
                backup_root=fx.backup_root,
                files_may_have_changed=True,
            )
            self.assertGreater(current["generation"], stale["generation"])
            with self.assertRaises(StaleJournalWriterError):
                transition_journal(
                    fx.root,
                    stale,
                    "rollback_in_progress",
                    backup_root=fx.backup_root,
                )

    def test_schema_one_migrates_then_persists_through_cas(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            payload = {
                "schema_version": 1,
                "attempt_id": "a" * 32,
                "generation": 3,
                "state": "validation_completed",
                "recovery_required": True,
                "backup_path": "attempt-1",
                "created_at": "x",
                "updated_at": "x",
                "dependencies_may_have_changed": False,
                "files_rollback_attempted": True,
                "files_rollback_ok": True,
                "remaining_processes": [],
                "errors": [],
            }
            journal_path(fx.root).parent.mkdir(parents=True, exist_ok=True)
            journal_path(fx.root).write_text(json.dumps(payload), encoding="utf-8")
            migrated = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["state"], "rollback_validation_completed")
            current = transition_journal(
                fx.root,
                migrated,
                "cleared",
                backup_root=fx.backup_root,
            )
            self.assertEqual(current["schema_version"], 2)
            self.assertEqual(current["state"], "cleared")


class RecoveryFlowTests(unittest.TestCase):
    def test_incomplete_transaction_restores_exact_files_and_launches_once(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            transition_journal(
                fx.root,
                journal,
                "files_applying",
                backup_root=fx.backup_root,
                files_may_have_changed=True,
            )
            expected_modified = _sha(fx.backup / "files" / "modified.txt")
            expected_deleted = _sha(fx.backup / "files" / "deleted.txt")
            expected_managed = _sha(fx.backup / "control" / "managed_files.json")
            launches = []
            result = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                validator=lambda *_args: (True, "validated"),
                launcher=lambda command, **kwargs: launches.append((command, kwargs)),
                launch_after_success=True,
            )
            self.assertTrue(result.recovered)
            self.assertTrue(result.launched)
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0][0][-1], "--post-recovery")
            self.assertEqual(_sha(fx.root / "modified.txt"), expected_modified)
            self.assertEqual(_sha(fx.root / "deleted.txt"), expected_deleted)
            self.assertFalse((fx.root / "created.txt").exists())
            self.assertEqual(_sha(fx.root / "updater" / "managed_files.json"), expected_managed)
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "cleared")
            second_launch = mock.Mock()
            second = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                launcher=second_launch,
                launch_after_success=True,
            )
            self.assertTrue(second.continue_startup)
            second_launch.assert_not_called()

    def test_crash_mid_rollback_resumes_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            transition_journal(
                fx.root,
                journal,
                "files_applying",
                backup_root=fx.backup_root,
                files_may_have_changed=True,
            )
            def partial(root, backup, **_kwargs):
                (Path(root) / "modified.txt").write_text("old-modified", encoding="utf-8")
                raise RuntimeError("simulated crash")
            first = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                restore_func=partial,
                validator=lambda *_args: (True, "ok"),
                launch_after_success=False,
            )
            self.assertTrue(first.pending)
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "rollback_in_progress")
            second = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                validator=lambda *_args: (True, "ok"),
                launch_after_success=False,
            )
            self.assertTrue(second.recovered)
            self.assertTrue(second.continue_startup)
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "cleared")

    def test_dependency_validation_failure_enters_repair_required(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            journal = transition_journal(fx.root, journal, "files_applying", backup_root=fx.backup_root, files_may_have_changed=True)
            journal = transition_journal(fx.root, journal, "files_applied", backup_root=fx.backup_root)
            transition_journal(fx.root, journal, "dependencies_starting", backup_root=fx.backup_root, dependencies_may_have_changed=True)
            result = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                validator=lambda *_args: (False, "dependency_version_changed:alpha"),
                launch_after_success=False,
            )
            self.assertTrue(result.pending)
            self.assertEqual(result.state, "dependency_repair_required")
            self.assertEqual(load_journal(fx.root, backup_root=fx.backup_root)["state"], "dependency_repair_required")
            self.assertTrue(fx.backup.exists())


class RecoveryJournalSafetyTests(unittest.TestCase):
    def test_truncated_or_unknown_journal_fails_closed(self):
        for text in ('{"schema_version":2,"attempt_id":', json.dumps({"schema_version": 99})):
            with self.subTest(text=text[:20]), tempfile.TemporaryDirectory() as td:
                fx = RecoveryFixture(Path(td))
                journal_path(fx.root).parent.mkdir(parents=True, exist_ok=True)
                journal_path(fx.root).write_text(text, encoding="utf-8")
                result = recover_pending(
                    fx.root,
                    backup_root=fx.backup_root,
                    launch_after_success=False,
                )
                self.assertTrue(result.pending)
                self.assertEqual(result.exit_code, RECOVERY_REQUIRED_EXIT_CODE)

    def test_backup_traversal_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            payload = {
                "schema_version": 2,
                "attempt_id": "a" * 32,
                "generation": 1,
                "state": "waiting_for_processes",
                "recovery_required": True,
                "backup_path": "../outside",
                "remaining_processes": [],
                "errors": [],
            }
            journal_path(fx.root).parent.mkdir(parents=True, exist_ok=True)
            journal_path(fx.root).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RecoveryJournalError):
                load_journal(fx.root, backup_root=fx.backup_root)
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
                self.skipTest("symlink unavailable")
            payload = {
                "schema_version": 2,
                "attempt_id": "a" * 32,
                "generation": 1,
                "state": "waiting_for_processes",
                "recovery_required": True,
                "backup_path": "escape",
                "remaining_processes": [],
                "errors": [],
            }
            journal_path(fx.root).write_text(json.dumps(payload), encoding="utf-8")
            journal = load_journal(fx.root, backup_root=fx.backup_root)
            with self.assertRaises(RecoveryJournalError):
                resolve_backup(fx.root, journal, backup_root=fx.backup_root)

    def test_updater_gate_does_not_rewrite_journal_while_process_is_alive(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine([{"pid": 44, "creation_time": 900, "role": "pip_root_or_descendant"}])
            before = journal_path(fx.root).read_bytes()
            result = updater_recovery_gate(
                fx.root,
                backup_root=fx.backup_root,
                inspector=lambda row: ("alive", ""),
            )
            self.assertTrue(result.pending)
            self.assertEqual(result.exit_code, 7)
            self.assertEqual(journal_path(fx.root).read_bytes(), before)
            self.assertTrue(fx.backup.exists())


if __name__ == "__main__":
    unittest.main()
