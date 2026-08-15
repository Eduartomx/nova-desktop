from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from updater import recovery_bootstrap, recovery_locking
from updater.recovery_state import (
    StaleJournalWriterError,
    create_transaction_journal,
    load_journal,
    prepare_stable_recovery_runtime,
    restore_backup_idempotent,
    transition_journal,
)


REPO = Path(__file__).resolve().parents[1]
NOVA = REPO / "nova"
STABLE_MODULES = (
    "recovery_journal.py", "recovery_attempts.py", "recovery_files.py",
    "recovery_environment.py", "recovery_state.py", "recovery_locking.py",
    "recovery_handoff.py", "recovery_bootstrap.py",
)


def _file_lock_factories(lock_dir: str):
    base = Path(lock_dir)
    return {
        "supervisor": lambda: recovery_locking._ScopedFileLock(base / "supervisor.lock"),
        "runtime": lambda: recovery_locking._ScopedFileLock(base / "runtime.lock"),
    }


def _recovery_worker(root_s, backup_root_s, lock_dir_s, entered, release, results, launch_log_s, hold):
    root = Path(root_s); backup_root = Path(backup_root_s); launch_log = Path(launch_log_s)
    def restore(root_arg, backup_arg, **_kwargs):
        entered.set()
        if hold:
            release.wait(20)
        restore_backup_idempotent(root_arg, backup_arg, backup_root=backup_root)
    def launch(_command, **_kwargs):
        with open(launch_log, "a", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()}\n")
        return object()
    result = recovery_bootstrap.recover_pending(
        root,
        backup_root=backup_root,
        restore_func=restore,
        validator=lambda *_args: (True, "ok"),
        launcher=launch,
        launch_after_success=True,
        lock_factories=_file_lock_factories(lock_dir_s),
    )
    results.put((os.getpid(), result.pending, result.exit_code, result.state, result.recovered, result.launched))


def _resume_worker(root_s, backup_root_s, lock_dir_s, results):
    root = Path(root_s); backup_root = Path(backup_root_s)
    result = recovery_bootstrap.recover_pending(
        root,
        backup_root=backup_root,
        validator=lambda *_args: (True, "ok"),
        launch_after_success=False,
        lock_factories=_file_lock_factories(lock_dir_s),
    )
    results.put((result.pending, result.exit_code, result.state, result.recovered))


class RecoveryMultiprocessTests(unittest.TestCase):
    def _fixture(self, td: str):
        root = Path(td) / "nova"
        backup_root = root / "data" / "updater_backups"
        backup = backup_root / "attempt"
        (root / "updater").mkdir(parents=True)
        (backup / "files").mkdir(parents=True)
        (backup / "control").mkdir()
        (root / "app.py").write_text("VALUE=1\n", encoding="utf-8")
        (root / "updater" / "nova_updater.py").write_text("VALUE=1\n", encoding="utf-8")
        (root / "updater" / "update_runner.py").write_text("VALUE=1\n", encoding="utf-8")
        for name in STABLE_MODULES:
            shutil.copy2(NOVA / "updater" / name, root / "updater" / name)
        (root / "updater" / "managed_files.json").write_text('{"files":["f.txt"]}', encoding="utf-8")
        (backup / "control" / "managed_files.json").write_text('{"files":["f.txt"]}', encoding="utf-8")
        (root / "f.txt").write_text("new", encoding="utf-8")
        (backup / "files" / "f.txt").write_text("old", encoding="utf-8")
        (backup / "backup.json").write_text(json.dumps({
            "schema": 2, "from": "old", "to": "new",
            "modified_existing": ["f.txt"], "deleted_existing": [], "created_new": [], "unchanged": [],
            "managed_files": {"path": "updater/managed_files.json", "existed": True, "backup": "control/managed_files.json"},
        }), encoding="utf-8")
        prepare_stable_recovery_runtime(root)
        journal = create_transaction_journal(root, backup, backup_root=backup_root)
        journal = transition_journal(root, journal, "files_applying", backup_root=backup_root, files_may_have_changed=True)
        return root, backup_root, journal

    def test_two_real_processes_only_one_restores_validates_and_spawns_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup_root, stale = self._fixture(td)
            lock_dir = Path(td) / "locks"; lock_dir.mkdir()
            launch_log = Path(td) / "launch.log"
            ctx = multiprocessing.get_context("spawn")
            entered = ctx.Event(); release = ctx.Event(); results = ctx.Queue()
            first = ctx.Process(target=_recovery_worker, args=(str(root), str(backup_root), str(lock_dir), entered, release, results, str(launch_log), True))
            first.start()
            self.assertTrue(entered.wait(15), "first recovery did not enter restore")
            second_entered = ctx.Event(); second_release = ctx.Event()
            second = ctx.Process(target=_recovery_worker, args=(str(root), str(backup_root), str(lock_dir), second_entered, second_release, results, str(launch_log), False))
            second.start(); second.join(15)
            self.assertFalse(second.is_alive(), "second recovery did not return while lock was held")
            second_result = results.get(timeout=5)
            self.assertTrue(second_result[1])
            self.assertEqual(second_result[2], 7)
            release.set(); first.join(20)
            self.assertFalse(first.is_alive())
            first_result = results.get(timeout=5)
            self.assertTrue(first_result[4])
            self.assertTrue(first_result[5])
            self.assertEqual((root / "f.txt").read_text(encoding="utf-8"), "old")
            launches = launch_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(launches), 1)
            current = load_journal(root, backup_root=backup_root)
            self.assertEqual(current["state"], "cleared")
            with self.assertRaises(StaleJournalWriterError):
                transition_journal(root, stale, "rollback_in_progress", backup_root=backup_root)

    def test_killing_lock_owner_allows_next_process_to_resume_durable_state(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup_root, _stale = self._fixture(td)
            lock_dir = Path(td) / "locks"; lock_dir.mkdir()
            ctx = multiprocessing.get_context("spawn")
            entered = ctx.Event(); never = ctx.Event(); results = ctx.Queue(); launch_log = Path(td) / "launch.log"
            owner = ctx.Process(target=_recovery_worker, args=(str(root), str(backup_root), str(lock_dir), entered, never, results, str(launch_log), True))
            owner.start()
            self.assertTrue(entered.wait(15), "owner never acquired recovery locks")
            owner.terminate(); owner.join(15)
            self.assertFalse(owner.is_alive())
            resumed = ctx.Process(target=_resume_worker, args=(str(root), str(backup_root), str(lock_dir), results))
            resumed.start(); resumed.join(20)
            self.assertFalse(resumed.is_alive())
            result = results.get(timeout=5)
            self.assertFalse(result[0])
            self.assertTrue(result[3])
            self.assertEqual(load_journal(root, backup_root=backup_root)["state"], "cleared")
            self.assertEqual((root / "f.txt").read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
