from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from updater import nova_updater, resident_update_engine, update_runner
from updater.recovery_bootstrap import recover_pending, updater_recovery_gate
from updater.recovery_state import RECOVERY_REQUIRED_EXIT_CODE, RecoveryResult, journal_path, load_journal
from tests.test_recovery_bootstrap import RecoveryFixture, _TestLock, _ExclusiveState


class PublicUpdaterGateTests(unittest.TestCase):
    def test_public_yes_still_delegates_to_supervisor_and_never_syncs_directly(self):
        with mock.patch.object(nova_updater, "_delegate_direct_update", return_value=7) as delegated, \
             mock.patch.object(nova_updater, "sync_release") as sync:
            rc = nova_updater.main(["--yes"])
        self.assertEqual(rc, 7)
        delegated.assert_called_once()
        sync.assert_not_called()

    def test_resident_engine_active_journal_blocks_before_github_or_pip(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine([{"pid": 44, "creation_time": 900, "role": "pip_root_or_descendant"}])
            with mock.patch.object(resident_update_engine.base, "ROOT", fx.root), \
                 mock.patch.object(resident_update_engine.base, "get_release") as get_release, \
                 mock.patch.object(resident_update_engine.base, "sync_release") as sync_release, \
                 mock.patch.object(resident_update_engine, "_install_requirements") as pip_install:
                rc = resident_update_engine.main(["--yes"])
        self.assertEqual(rc, RECOVERY_REQUIRED_EXIT_CODE)
        get_release.assert_not_called()
        sync_release.assert_not_called()
        pip_install.assert_not_called()

    def test_resident_engine_corrupt_journal_blocks_before_github_or_pip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "nova"
            (root / "data").mkdir(parents=True)
            journal_path(root).write_text('{"schema_version":', encoding="utf-8")
            with mock.patch.object(resident_update_engine.base, "ROOT", root), \
                 mock.patch.object(resident_update_engine.base, "get_release") as get_release, \
                 mock.patch.object(resident_update_engine.base, "sync_release") as sync_release, \
                 mock.patch.object(resident_update_engine, "_install_requirements") as pip_install:
                rc = resident_update_engine.main(["--yes"])
        self.assertEqual(rc, RECOVERY_REQUIRED_EXIT_CODE)
        get_release.assert_not_called()
        sync_release.assert_not_called()
        pip_install.assert_not_called()

    def test_setup_failure_before_pip_start_allows_file_rollback_without_quarantine(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "install"
            stage = base / "stage"
            backups = root / "data" / "updater_backups"
            (root / "updater").mkdir(parents=True)
            stage.mkdir()
            (root / "requirements.txt").write_text("old-package==1\n", encoding="utf-8")
            (stage / "requirements.txt").write_text("new-package==2\n", encoding="utf-8")
            managed = root / "updater" / "managed_files.json"
            managed.write_text(json.dumps({"tag": "old", "files": ["requirements.txt"]}), encoding="utf-8")
            old_bytes = (root / "requirements.txt").read_bytes()
            error = resident_update_engine.DependencyInstallError(
                "job setup failed before launch", dependency_started=False
            )
            with mock.patch.object(resident_update_engine.base, "ROOT", root), \
                 mock.patch.object(resident_update_engine.base, "MANAGED_PATH", managed), \
                 mock.patch.object(resident_update_engine, "_install_requirements", side_effect=error), \
                 mock.patch.object(resident_update_engine.base, "validate_install") as validate:
                with self.assertRaises(resident_update_engine.DependencyInstallError):
                    resident_update_engine.execute_transaction(
                        stage,
                        ["requirements.txt"],
                        {"requirements.txt"},
                        "v-new", "old", "new",
                        backup_root=backups,
                    )
            validate.assert_not_called()
            self.assertEqual((root / "requirements.txt").read_bytes(), old_bytes)
            self.assertFalse(journal_path(root).exists())


class SupervisorRecoveryGateTests(unittest.TestCase):
    class _Guard:
        def __init__(self):
            self.release_calls = 0
        def release(self):
            self.release_calls += 1

    class _Supervisor:
        def __init__(self):
            self.release_calls = 0
        def acquire(self):
            return True
        def release(self):
            self.release_calls += 1

    def test_existing_quarantine_returns_seven_before_runtime_coordination(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            supervisor = self._Supervisor()
            pending = RecoveryResult(True, 7, "waiting_for_processes", "pending")
            with mock.patch.object(update_runner, "nova_root", return_value=root), \
                 mock.patch.object(update_runner, "_recovery_gate", return_value=pending), \
                 mock.patch.object(update_runner, "coordinate_runtime_shutdown") as coordinate, \
                 mock.patch.object(update_runner, "run_update") as run_update, \
                 mock.patch.object(update_runner, "write_status") as status, \
                 mock.patch.object(update_runner, "launch_nova") as launch:
                rc = update_runner.main([], supervisor_lock_factory=lambda: supervisor)
        self.assertEqual(rc, RECOVERY_REQUIRED_EXIT_CODE)
        coordinate.assert_not_called()
        run_update.assert_not_called()
        status.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(supervisor.release_calls, 1)

    def test_code_seven_with_durable_journal_releases_runtime_guard_but_never_launches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir(parents=True)
            supervisor = self._Supervisor()
            guard = self._Guard()
            coordination = update_runner.ShutdownCoordination(
                True, process_terminated=True, lock_acquired=True, guard=guard
            )

            def fake_update(_root, _log):
                journal_path(root).write_text('{"quarantine":"present"}', encoding="utf-8")
                return 7, ""

            with mock.patch.object(update_runner, "nova_root", return_value=root), \
                 mock.patch.object(update_runner, "_recovery_gate", return_value=RecoveryResult(False, 0, "", "", continue_startup=True)), \
                 mock.patch.object(update_runner, "coordinate_runtime_shutdown", return_value=coordination), \
                 mock.patch.object(update_runner, "run_update", side_effect=fake_update), \
                 mock.patch.object(update_runner, "read_version", side_effect=["0.9.8", "0.9.8"]), \
                 mock.patch.object(update_runner, "write_status"), \
                 mock.patch.object(update_runner, "launch_nova") as launch:
                rc = update_runner.main([], supervisor_lock_factory=lambda: supervisor)
        self.assertEqual(rc, RECOVERY_REQUIRED_EXIT_CODE)
        self.assertEqual(guard.release_calls, 1)
        self.assertEqual(supervisor.release_calls, 1)
        launch.assert_not_called()


class RecoveryStateTransitionTests(unittest.TestCase):
    def test_live_identity_persists_waiting_for_processes_once(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine([{"pid": 88, "creation_time": 1001, "role": "pip_root_or_descendant"}])
            before = load_journal(fx.root, backup_root=fx.backup_root)
            result = updater_recovery_gate(
                fx.root,
                backup_root=fx.backup_root,
                inspector=lambda row: ("alive", ""),
            )
            after = load_journal(fx.root, backup_root=fx.backup_root)
        self.assertTrue(result.pending)
        self.assertEqual(result.exit_code, RECOVERY_REQUIRED_EXIT_CODE)
        self.assertEqual(after["state"], "waiting_for_processes")
        self.assertEqual(after["generation"], before["generation"] + 1)

    def test_post_recovery_launch_failure_restores_quarantine_at_validation_completed(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine()
            result = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                validator=lambda root: (True, "validated"),
                launcher=mock.Mock(side_effect=OSError("launch unavailable")),
                launch_after_success=True,
            )
            journal = load_journal(fx.root, backup_root=fx.backup_root)
        self.assertTrue(result.pending)
        self.assertTrue(result.recovered)
        self.assertFalse(result.launched)
        self.assertEqual(result.exit_code, RECOVERY_REQUIRED_EXIT_CODE)
        self.assertEqual(journal["state"], "validation_completed")
        self.assertTrue(journal["recovery_required"])
        self.assertTrue(fx.backup.exists())

    def test_retry_from_validation_completed_skips_rollback_and_launches_once(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            fx.quarantine()
            first = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                validator=lambda root: (True, "validated"),
                launcher=mock.Mock(side_effect=OSError("launch unavailable")),
                launch_after_success=True,
            )
            self.assertTrue(first.pending)
            restore = mock.Mock(side_effect=AssertionError("rollback must not repeat after validation_completed"))
            launches = []
            second = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                restore_func=restore,
                validator=lambda root: (True, "validated"),
                launcher=lambda command, **kwargs: launches.append(command),
                launch_after_success=True,
            )
            journal = load_journal(fx.root, backup_root=fx.backup_root)
        restore.assert_not_called()
        self.assertTrue(second.recovered)
        self.assertTrue(second.launched)
        self.assertEqual(len(launches), 1)
        self.assertEqual(journal["state"], "cleared")


if __name__ == "__main__":
    unittest.main()
