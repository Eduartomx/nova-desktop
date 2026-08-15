from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from updater import nova_updater, resident_update_engine, update_runner
from updater.recovery_bootstrap import recover_pending
from updater.recovery_state import (
    RECOVERY_REQUIRED_EXIT_CODE,
    RecoveryResult,
    create_transaction_journal,
    journal_path,
    load_journal,
    transition_journal,
)
from tests.test_recovery_bootstrap import RecoveryFixture


class PublicUpdaterGateTests(unittest.TestCase):
    def test_public_yes_still_delegates_to_supervisor_and_never_syncs_directly(self):
        with mock.patch.object(nova_updater, "_delegate_direct_update", return_value=7) as delegated, \
             mock.patch.object(nova_updater, "sync_release") as sync:
            rc = nova_updater.main(["--yes"])
        self.assertEqual(rc, 7); delegated.assert_called_once(); sync.assert_not_called()

    def test_resident_engine_direct_cli_is_always_blocked_before_github_or_pip(self):
        with mock.patch.object(resident_update_engine.base, "get_release") as get_release, \
             mock.patch.object(resident_update_engine.base, "sync_release") as sync_release, \
             mock.patch.object(resident_update_engine, "_install_requirements") as pip_install:
            rc = resident_update_engine.main(["--yes"])
        self.assertEqual(rc, 4)
        get_release.assert_not_called(); sync_release.assert_not_called(); pip_install.assert_not_called()

    def test_internal_engine_active_journal_blocks_before_github_or_pip(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td)); fx.quarantine([{"pid": 44, "creation_time": 900, "role": "pip_root_or_descendant"}])
            with mock.patch.object(resident_update_engine.base, "get_release") as get_release, \
                 mock.patch.object(resident_update_engine.base, "sync_release") as sync_release, \
                 mock.patch.object(resident_update_engine, "_install_requirements") as pip_install:
                rc = resident_update_engine.run_supervised_update(fx.root)
        self.assertEqual(rc, RECOVERY_REQUIRED_EXIT_CODE)
        get_release.assert_not_called(); sync_release.assert_not_called(); pip_install.assert_not_called()

    def test_setup_failure_before_pip_start_rolls_back_then_clears_durable_journal(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); root = base / "install"; stage = base / "stage"; backups = root / "data" / "updater_backups"
            (root / "updater").mkdir(parents=True); stage.mkdir(); backups.mkdir(parents=True)
            (root / "requirements.txt").write_text("old-package==1\n", encoding="utf-8")
            (stage / "requirements.txt").write_text("new-package==2\n", encoding="utf-8")
            managed = root / "updater" / "managed_files.json"
            managed.write_text(json.dumps({"tag": "old", "files": ["requirements.txt"]}), encoding="utf-8")
            (root / "app.py").write_text("VALUE=1\n", encoding="utf-8")
            (root / "updater" / "nova_updater.py").write_text("VALUE=1\n", encoding="utf-8")
            (root / "updater" / "update_runner.py").write_text("VALUE=1\n", encoding="utf-8")
            # Stable recovery bundle sources are required before file application.
            source = Path(__file__).resolve().parents[1] / "nova" / "updater"
            for module in (
                "process_launch.py",
                "recovery_journal.py", "recovery_attempts.py", "recovery_files.py",
                "recovery_environment.py", "recovery_state.py", "recovery_locking.py",
                "recovery_handoff.py", "recovery_bootstrap.py",
            ):
                (root / "updater" / module).write_bytes((source / module).read_bytes())
            old_bytes = (root / "requirements.txt").read_bytes()
            error = resident_update_engine.DependencyInstallError("job setup failed before launch", dependency_started=False)
            with mock.patch.object(resident_update_engine.base, "ROOT", root), \
                 mock.patch.object(resident_update_engine.base, "MANAGED_PATH", managed), \
                 mock.patch.object(resident_update_engine, "_install_requirements", side_effect=error), \
                 mock.patch.object(resident_update_engine, "capture_dependency_snapshot", return_value=("control/dependency_snapshot.json", "0" * 64)), \
                 mock.patch.object(resident_update_engine, "validate_restored_install", return_value=(True, "restored")), \
                 mock.patch.object(resident_update_engine.base, "validate_install") as validate:
                with self.assertRaises(resident_update_engine.DependencyInstallError):
                    resident_update_engine.execute_transaction(
                        stage, ["requirements.txt"], {"requirements.txt"}, "v-new", "old", "new", backup_root=backups,
                    )
            validate.assert_not_called()
            self.assertEqual((root / "requirements.txt").read_bytes(), old_bytes)
            journal = load_journal(root, backup_root=backups)
            self.assertIsNotNone(journal)
            self.assertEqual(journal["state"], "rollback_validation_completed")
            self.assertTrue(journal["recovery_required"])
            self.assertFalse(journal["dependencies_may_have_changed"])


class SupervisorRecoveryGateTests(unittest.TestCase):
    class _Guard:
        def __init__(self): self.release_calls = 0
        def release(self): self.release_calls += 1
    class _Supervisor:
        def __init__(self): self.release_calls = 0
        def acquire(self): return True
        def release(self): self.release_calls += 1

    def test_existing_quarantine_returns_seven_before_runtime_coordination(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); supervisor = self._Supervisor(); pending = RecoveryResult(True, 7, "waiting_for_processes", "pending")
            with mock.patch.object(update_runner, "nova_root", return_value=root), \
                 mock.patch.object(update_runner, "_recovery_gate", return_value=pending), \
                 mock.patch.object(update_runner, "coordinate_runtime_shutdown") as coordinate, \
                 mock.patch.object(update_runner, "run_update") as run_update, \
                 mock.patch.object(update_runner, "write_status") as status, \
                 mock.patch.object(update_runner, "launch_nova") as launch:
                rc = update_runner.main([], supervisor_lock_factory=lambda: supervisor)
        self.assertEqual(rc, 7); coordinate.assert_not_called(); run_update.assert_not_called(); status.assert_not_called(); launch.assert_not_called()
        self.assertEqual(supervisor.release_calls, 1)

    def test_abnormal_engine_exit_with_active_journal_returns_seven_and_never_launches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "data" / "updater_backups").mkdir(parents=True); (root / "updater").mkdir()
            backup = root / "data" / "updater_backups" / "a"; (backup / "files").mkdir(parents=True); (backup / "control").mkdir()
            (backup / "control" / "managed_files.json").write_text('{"files":[]}', encoding="utf-8")
            (backup / "backup.json").write_text(json.dumps({"schema":2,"from":"x","to":"y","modified_existing":[],"deleted_existing":[],"created_new":[],"unchanged":[],"managed_files":{"path":"updater/managed_files.json","existed":True,"backup":"control/managed_files.json"}}), encoding="utf-8")
            (root / "updater" / "managed_files.json").write_text('{"files":[]}', encoding="utf-8")
            journal = create_transaction_journal(root, backup)
            journal = transition_journal(root, journal, "files_applying", files_may_have_changed=True)
            supervisor = self._Supervisor(); guard = self._Guard()
            coordination = update_runner.ShutdownCoordination(True, process_terminated=True, lock_acquired=True, guard=guard)
            with mock.patch.object(update_runner, "nova_root", return_value=root), \
                 mock.patch.object(update_runner, "_recovery_gate", return_value=RecoveryResult(False, 0, "", "", continue_startup=True)), \
                 mock.patch.object(update_runner, "coordinate_runtime_shutdown", return_value=coordination), \
                 mock.patch.object(update_runner, "run_update", return_value=(2, "crash")), \
                 mock.patch.object(update_runner, "read_version", return_value="0.9.8"), \
                 mock.patch.object(update_runner, "write_status"), \
                 mock.patch.object(update_runner, "launch_nova") as launch:
                rc = update_runner.main([], supervisor_lock_factory=lambda: supervisor)
        self.assertEqual(rc, 7); launch.assert_not_called(); self.assertEqual(guard.release_calls, 1)


class RecoveryStateTransitionTests(unittest.TestCase):
    def test_startup_recovery_persists_waiting_under_recovery_locks(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td)); fx.quarantine([{"pid": 88, "creation_time": 1001, "role": "pip_root_or_descendant"}])
            before = load_journal(fx.root, backup_root=fx.backup_root)
            result = recover_pending(fx.root, backup_root=fx.backup_root, inspector=lambda row: ("alive", ""), launch_after_success=False)
            after = load_journal(fx.root, backup_root=fx.backup_root)
        self.assertTrue(result.pending); self.assertEqual(result.exit_code, 7)
        self.assertEqual(after["state"], "waiting_for_processes"); self.assertEqual(after["generation"], before["generation"] + 1)

    def test_post_recovery_helper_spawn_failure_keeps_validated_quarantine(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td)); journal = fx.transaction()
            journal = transition_journal(fx.root, journal, "files_applying", backup_root=fx.backup_root, files_may_have_changed=True)
            result = recover_pending(
                fx.root, backup_root=fx.backup_root,
                validator=lambda *_args: (True, "validated"),
                launcher=mock.Mock(side_effect=OSError("launch unavailable")), launch_after_success=True,
            )
            current = load_journal(fx.root, backup_root=fx.backup_root)
        self.assertTrue(result.pending); self.assertTrue(result.recovered); self.assertFalse(result.launched)
        self.assertEqual(current["state"], "rollback_validation_completed"); self.assertTrue(current["recovery_required"])

    def test_retry_from_validated_state_skips_rollback_and_spawns_one_helper(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td)); journal = fx.transaction()
            journal = transition_journal(fx.root, journal, "files_applying", backup_root=fx.backup_root, files_may_have_changed=True)
            first = recover_pending(fx.root, backup_root=fx.backup_root, validator=lambda *_args: (True, "validated"), launcher=mock.Mock(side_effect=OSError("launch unavailable")), launch_after_success=True)
            self.assertTrue(first.pending)
            restore = mock.Mock(side_effect=AssertionError("rollback must not repeat")); launches = []
            second = recover_pending(
                fx.root, backup_root=fx.backup_root, restore_func=restore,
                validator=lambda *_args: (True, "validated"),
                launcher=lambda command, **kwargs: launches.append(command) or object(), launch_after_success=True,
            )
            current = load_journal(fx.root, backup_root=fx.backup_root)
        restore.assert_not_called(); self.assertTrue(second.recovered); self.assertTrue(second.launched)
        self.assertEqual(len(launches), 1); self.assertIn("--handoff-launch", launches[0]); self.assertEqual(current["state"], "cleared")


if __name__ == "__main__":
    unittest.main()
