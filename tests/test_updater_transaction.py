from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from updater import nova_updater, pip_safety
from updater.pip_safety import PipTerminationResult


def snapshot_tree(root: Path) -> dict[str, str]:
    result = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_file(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class _FakePipProcess:
    def __init__(self, wait_effects, *, pid=4321, terminate_error=None, kill_error=None):
        self.pid = pid
        self.wait_effects = list(wait_effects)
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self):
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if not self.wait_effects:
            raise AssertionError("unexpected extra wait")
        effect = self.wait_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        self.returncode = int(effect)
        return self.returncode

    def poll(self):
        return self.returncode


class _FakeTreeAPI:
    def __init__(self, *, snapshots=None, alive=None, terminate_error=None, kill_error=None):
        self.snapshots = list(snapshots or [])
        self.alive_results = list(alive or [])
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.snapshot_calls = 0
        self.terminate_calls = []
        self.kill_calls = []
        self.alive_calls = []

    def snapshot(self, root_pid):
        self.snapshot_calls += 1
        if self.snapshots:
            value = self.snapshots.pop(0)
            if isinstance(value, BaseException):
                raise value
            return set(value)
        return {int(root_pid)}

    def terminate(self, root_pid, pids):
        self.terminate_calls.append((int(root_pid), set(pids)))
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self, root_pid, pids):
        self.kill_calls.append((int(root_pid), set(pids)))
        if self.kill_error is not None:
            raise self.kill_error

    def alive(self, pids):
        self.alive_calls.append(set(pids))
        if self.alive_results:
            value = self.alive_results.pop(0)
            if isinstance(value, BaseException):
                raise value
            return set(value)
        return set()


class UpdaterTransactionTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root = base / "install"
        stage = base / "stage"
        backups = base / "backups"
        root.mkdir()
        stage.mkdir()

        original = {
            "assistant/modified.py": b"modified-old\n",
            "assistant/deleted.py": b"deleted-old\x00\xff",
            "assistant/unchanged.py": b"unchanged-same\n",
            "requirements.txt": b"package-old==1\n",
        }
        for rel, data in original.items():
            write_file(root, rel, data)

        new = {
            "assistant/modified.py": b"modified-new\n",
            "assistant/unchanged.py": original["assistant/unchanged.py"],
            "assistant/created.py": b"created-new\n",
            "requirements.txt": original["requirements.txt"],
        }
        for rel, data in new.items():
            write_file(stage, rel, data)

        managed = root / "updater" / "managed_files.json"
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text(
            json.dumps({"tag": "v-old", "files": sorted(original)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return base, root, stage, backups, managed, original, new

    def patch_install(self, root: Path, managed: Path):
        return mock.patch.multiple(nova_updater, ROOT=root, MANAGED_PATH=managed)

    def latest_backup(self, backups: Path) -> Path:
        rows = sorted(path for path in backups.iterdir() if path.is_dir())
        self.assertTrue(rows)
        return rows[-1]

    def assert_previous_tree_preserved_with_recovery_marker(self, root: Path, before: dict[str, str]):
        after = snapshot_tree(root)
        for rel, digest in before.items():
            self.assertEqual(after.get(rel), digest, rel)
        self.assertEqual(set(after) - set(before), {"data/update_recovery.json"})

    @staticmethod
    def confirmed_termination():
        return PipTerminationResult(True, True, [], [], "terminación de pip y descendientes confirmada")

    def test_validation_failure_restores_tree_byte_for_byte_without_dependency_uncertainty(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        before = snapshot_tree(root)
        previous = set(original)
        new_files = list(new)
        with self.patch_install(root, managed), \
             mock.patch.object(nova_updater, "validate_install", return_value=(False, "injected validation failure")):
            with self.assertRaisesRegex(RuntimeError, "validation failure"):
                nova_updater.execute_transaction(stage, new_files, previous, "v-new", "old", "new", backup_root=backups)
        after = snapshot_tree(root)
        self.assertEqual(after, before)
        self.assertIn("assistant/unchanged.py", after)
        self.assertNotIn("assistant/created.py", after)
        self.assertEqual((root / "assistant/modified.py").read_bytes(), original["assistant/modified.py"])
        self.assertEqual((root / "assistant/deleted.py").read_bytes(), original["assistant/deleted.py"])
        status = json.loads((self.latest_backup(backups) / "rollback_status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["files_rollback_ok"])
        self.assertFalse(status["dependencies_may_have_changed"])
        self.assertFalse(status["recovery_required"])
        self.assertFalse((root / "data" / "update_recovery.json").exists())

    def test_injected_failure_after_real_copies_rolls_back_without_touching_unchanged(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        before = snapshot_tree(root)
        previous = set(original)
        with self.patch_install(root, managed):
            manifest = nova_updater.build_transaction(stage, list(new), previous)
            self.assertEqual(manifest["modified_existing"], ["assistant/modified.py"])
            self.assertEqual(manifest["deleted_existing"], ["assistant/deleted.py"])
            self.assertEqual(manifest["created_new"], ["assistant/created.py"])
            self.assertIn("assistant/unchanged.py", manifest["unchanged"])
            backup = nova_updater.create_backup(manifest, "old", "new", backup_root=backups)
            real_replace = nova_updater._atomic_replace_from
            calls = {"count": 0}

            def fail_after_copy(src, dst):
                calls["count"] += 1
                real_replace(src, dst)
                if calls["count"] == 2:
                    raise RuntimeError("injected after copied file")

            with mock.patch.object(nova_updater, "_atomic_replace_from", side_effect=fail_after_copy):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    nova_updater.apply_transaction(stage, manifest)
            self.assertTrue((root / "assistant/created.py").is_file())
            self.assertEqual((root / "assistant/unchanged.py").read_bytes(), original["assistant/unchanged.py"])
            nova_updater.restore_backup(backup)
        self.assertEqual(snapshot_tree(root), before)

    def test_managed_files_previous_bytes_are_restored(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        managed_before = managed.read_bytes()
        with self.patch_install(root, managed):
            manifest = nova_updater.build_transaction(stage, list(new), set(original))
            backup = nova_updater.create_backup(manifest, "old", "new", backup_root=backups)
            nova_updater.apply_transaction(stage, manifest)
            nova_updater.write_managed(list(new), "v-new")
            self.assertNotEqual(managed.read_bytes(), managed_before)
            nova_updater.restore_backup(backup)
        self.assertEqual(managed.read_bytes(), managed_before)

    def test_pip_finishes_before_timeout_normally_without_shell(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text("example==1\n", encoding="utf-8")
            proc = _FakePipProcess([0])
            with mock.patch.object(nova_updater, "ROOT", root), \
                 mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc) as popen:
                nova_updater._install_requirements(timeout_seconds=12.5)
        popen.assert_called_once()
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertEqual(proc.wait_calls, [12.5])
        self.assertEqual(proc.terminate_calls, 0)
        self.assertEqual(proc.kill_calls, 0)

    def test_pip_timeout_validation_rejects_nonpositive_and_caps_absurd_values(self):
        with self.assertRaises(ValueError):
            nova_updater._normalize_pip_timeout(0)
        with self.assertRaises(ValueError):
            nova_updater._normalize_pip_timeout(-1)
        with self.assertRaises(ValueError):
            nova_updater._normalize_pip_timeout(float("inf"))
        self.assertEqual(
            nova_updater._normalize_pip_timeout(nova_updater.PIP_INSTALL_TIMEOUT_MAX_SECONDS * 100),
            nova_updater.PIP_INSTALL_TIMEOUT_MAX_SECONDS,
        )

    def test_pip_tree_timeout_then_terminate_succeeds_without_force(self):
        proc = _FakePipProcess([0])
        tree = _FakeTreeAPI(
            snapshots=[{4321, 4400}, {4321, 4400}, {4321, 4400}],
            alive=[set(), set()],
        )
        result = pip_safety.terminate_pip_tree(proc, 0.01, tree_api=tree)
        self.assertTrue(result.terminated_confirmed)
        self.assertTrue(result.direct_process_terminated)
        self.assertEqual(result.remaining_pids, [])
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(len(tree.terminate_calls), 1)
        self.assertEqual(len(tree.kill_calls), 0)

    def test_pip_tree_terminate_timeout_then_kill_succeeds(self):
        timeout = subprocess.TimeoutExpired(["pip"], 0.01)
        proc = _FakePipProcess([timeout, 0])
        tree = _FakeTreeAPI(
            snapshots=[{4321, 4400}, {4321, 4400}, {4321, 4400}, {4321, 4400}],
            alive=[{4321, 4400}, set()],
        )
        result = pip_safety.terminate_pip_tree(proc, 0.01, tree_api=tree)
        self.assertTrue(result.terminated_confirmed)
        self.assertEqual(proc.kill_calls, 1)
        self.assertEqual(len(tree.kill_calls), 1)
        self.assertTrue(any("wait_after_terminate:TimeoutExpired" in value for value in result.termination_errors))

    def test_pip_tree_terminate_exception_but_kill_succeeds(self):
        timeout = subprocess.TimeoutExpired(["pip"], 0.01)
        proc = _FakePipProcess([timeout, 0], terminate_error=OSError("terminate failed"))
        tree = _FakeTreeAPI(
            snapshots=[{4321}, {4321}, {4321}, {4321}],
            alive=[{4321}, set()],
        )
        result = pip_safety.terminate_pip_tree(proc, 0.01, tree_api=tree)
        self.assertTrue(result.terminated_confirmed)
        self.assertEqual(proc.kill_calls, 1)
        self.assertTrue(any("terminate_direct:OSError" in value for value in result.termination_errors))

    def test_pip_tree_kill_returns_but_process_still_alive_is_unconfirmed(self):
        timeout1 = subprocess.TimeoutExpired(["pip"], 0.01)
        timeout2 = subprocess.TimeoutExpired(["pip"], 0.01)
        proc = _FakePipProcess([timeout1, timeout2])
        tree = _FakeTreeAPI(
            snapshots=[{4321}, {4321}, {4321}, {4321}],
            alive=[{4321}, {4321}],
        )
        result = pip_safety.terminate_pip_tree(proc, 0.01, tree_api=tree)
        self.assertFalse(result.terminated_confirmed)
        self.assertFalse(result.direct_process_terminated)
        self.assertEqual(result.remaining_pids, [4321])
        self.assertEqual(proc.kill_calls, 1)

    def test_pip_tree_last_wait_timeout_is_unconfirmed(self):
        timeout1 = subprocess.TimeoutExpired(["pip"], 0.01)
        timeout2 = subprocess.TimeoutExpired(["pip"], 0.01)
        proc = _FakePipProcess([timeout1, timeout2])
        tree = _FakeTreeAPI(alive=[{4321}, set()])
        result = pip_safety.terminate_pip_tree(proc, 0.01, tree_api=tree)
        self.assertFalse(result.terminated_confirmed)
        self.assertFalse(result.direct_process_terminated)
        self.assertIn(4321, result.remaining_pids)
        self.assertTrue(any("wait_after_kill:TimeoutExpired" in value for value in result.termination_errors))

    def test_pip_tree_remaining_descendant_is_unconfirmed(self):
        timeout = subprocess.TimeoutExpired(["pip"], 0.01)
        proc = _FakePipProcess([timeout, 0])
        tree = _FakeTreeAPI(
            snapshots=[{4321, 4400}, {4321, 4400}, {4321, 4400}, {4321, 4400}],
            alive=[{4321, 4400}, {4400}],
        )
        result = pip_safety.terminate_pip_tree(proc, 0.01, tree_api=tree)
        self.assertFalse(result.terminated_confirmed)
        self.assertTrue(result.direct_process_terminated)
        self.assertEqual(result.remaining_pids, [4400])

    def test_confirmed_message_only_claims_stopped_and_waited_when_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text("example==1\n", encoding="utf-8")
            timeout = 0.25
            proc = _FakePipProcess([subprocess.TimeoutExpired(["pip"], timeout)])
            with mock.patch.object(nova_updater, "ROOT", root), \
                 mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc), \
                 mock.patch.object(nova_updater, "_terminate_timed_out_pip", return_value=self.confirmed_termination()):
                with self.assertRaises(RuntimeError) as caught:
                    nova_updater._install_requirements(timeout_seconds=timeout)
            self.assertIn("detenidos y esperados de forma verificable", str(caught.exception))

            proc2 = _FakePipProcess([subprocess.TimeoutExpired(["pip"], timeout)])
            unconfirmed = PipTerminationResult(False, False, [4321], ["wait_after_kill:TimeoutExpired"], "terminación de pip no confirmada")
            with mock.patch.object(nova_updater, "ROOT", root), \
                 mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc2), \
                 mock.patch.object(nova_updater, "_terminate_timed_out_pip", return_value=unconfirmed):
                with self.assertRaises(nova_updater.PipTerminationUnconfirmedError) as caught2:
                    nova_updater._install_requirements(timeout_seconds=timeout)
            self.assertNotIn("detenidos y esperados", str(caught2.exception))
            self.assertIn("no pudo confirmarse", str(caught2.exception))

    def test_pip_failure_after_start_restores_files_but_marks_dependency_recovery_required(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        before = snapshot_tree(root)
        with self.patch_install(root, managed), \
             mock.patch.object(nova_updater, "_install_requirements", side_effect=RuntimeError("simulated pip failure")) as install, \
             mock.patch.object(nova_updater, "validate_install") as validate:
            with self.assertRaisesRegex(RuntimeError, "entorno Python puede haber cambiado"):
                nova_updater.execute_transaction(stage, list(new), set(original), "v-new", "old", "new", backup_root=backups)
        install.assert_called_once_with()
        validate.assert_not_called()
        self.assert_previous_tree_preserved_with_recovery_marker(root, before)
        backup = self.latest_backup(backups)
        status = json.loads((backup / "rollback_status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["files_rollback_ok"])
        self.assertTrue(status["dependencies_may_have_changed"])
        self.assertTrue(status["recovery_required"])
        self.assertIn("requirements.txt no garantiza", status["message"])
        recovery = json.loads((root / "data" / "update_recovery.json").read_text(encoding="utf-8"))
        self.assertTrue(recovery["recovery_required"])
        self.assertTrue(backup.is_dir())

    def test_pip_timeout_confirmed_restores_sha_removes_created_restores_managed_and_preserves_backup(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        before = snapshot_tree(root)
        managed_before = managed.read_bytes()
        timeout = 0.25
        proc = _FakePipProcess([subprocess.TimeoutExpired(["pip"], timeout)])
        with self.patch_install(root, managed), \
             mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc) as popen, \
             mock.patch.object(nova_updater, "_terminate_timed_out_pip", return_value=self.confirmed_termination()), \
             mock.patch.object(nova_updater, "validate_install") as validate:
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                nova_updater.execute_transaction(
                    stage,
                    list(new),
                    set(original),
                    "v-new",
                    "old",
                    "new",
                    backup_root=backups,
                    pip_timeout_seconds=timeout,
                )
        validate.assert_not_called()
        popen.assert_called_once()
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assert_previous_tree_preserved_with_recovery_marker(root, before)
        self.assertFalse((root / "assistant" / "created.py").exists())
        self.assertEqual(managed.read_bytes(), managed_before)
        backup = self.latest_backup(backups)
        self.assertTrue(backup.is_dir())
        status = json.loads((backup / "rollback_status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["files_rollback_ok"])
        self.assertTrue(status["files_rollback_attempted"])
        self.assertTrue(status["dependencies_may_have_changed"])
        self.assertTrue(status["recovery_required"])
        recovery_path = root / "data" / "update_recovery.json"
        self.assertTrue(recovery_path.is_file())
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        self.assertTrue(recovery["files_rollback_ok"])
        self.assertTrue(recovery["dependencies_may_have_changed"])
        self.assertTrue(recovery["recovery_required"])

    def test_pip_timeout_unconfirmed_never_starts_rollback_and_preserves_backup_manifest(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        timeout = 0.25
        proc = _FakePipProcess([subprocess.TimeoutExpired(["pip"], timeout)])
        unconfirmed = PipTerminationResult(
            False,
            False,
            [4321, 4400],
            ["wait_after_kill:TimeoutExpired"],
            "terminación de pip no confirmada; PID restantes: 4321, 4400",
        )
        with self.patch_install(root, managed), \
             mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc), \
             mock.patch.object(nova_updater, "_terminate_timed_out_pip", return_value=unconfirmed), \
             mock.patch.object(nova_updater, "restore_backup", wraps=nova_updater.restore_backup) as restore:
            with self.assertRaises(nova_updater.PipTerminationUnconfirmedError):
                nova_updater.execute_transaction(
                    stage,
                    list(new),
                    set(original),
                    "v-new",
                    "old",
                    "new",
                    backup_root=backups,
                    pip_timeout_seconds=timeout,
                )
        restore.assert_not_called()
        backup = self.latest_backup(backups)
        self.assertTrue((backup / "backup.json").is_file())
        self.assertTrue(backup.is_dir())
        recovery = json.loads((root / "data" / "update_recovery.json").read_text(encoding="utf-8"))
        self.assertEqual(recovery["status"], "pip_termination_unconfirmed")
        self.assertIsNone(recovery["files_rollback_ok"])
        self.assertFalse(recovery["files_rollback_attempted"])
        self.assertTrue(recovery["dependencies_may_have_changed"])
        self.assertTrue(recovery["recovery_required"])
        self.assertEqual(recovery["remaining_pids"], [4321, 4400])
        self.assertNotIn("restaurados correctamente", recovery["message"])
        self.assertIn("no se inició", recovery["message"])

    def test_pip_timeout_with_partial_rollback_marks_both_failures_and_preserves_backup(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        timeout = 0.25
        proc = _FakePipProcess([subprocess.TimeoutExpired(["pip"], timeout)])
        real_replace = nova_updater._atomic_replace_from

        def fail_one_restore(src, dst):
            src = Path(src)
            dst = Path(dst)
            if backups in src.parents and dst == root / "assistant" / "modified.py":
                raise RuntimeError("injected restore failure")
            return real_replace(src, dst)

        with self.patch_install(root, managed), \
             mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc), \
             mock.patch.object(nova_updater, "_terminate_timed_out_pip", return_value=self.confirmed_termination()), \
             mock.patch.object(nova_updater, "_atomic_replace_from", side_effect=fail_one_restore):
            with self.assertRaisesRegex(RuntimeError, "rollback incompleto"):
                nova_updater.execute_transaction(
                    stage,
                    list(new),
                    set(original),
                    "v-new",
                    "old",
                    "new",
                    backup_root=backups,
                    pip_timeout_seconds=timeout,
                )
        backup = self.latest_backup(backups)
        self.assertTrue(backup.is_dir())
        status = json.loads((backup / "rollback_status.json").read_text(encoding="utf-8"))
        self.assertFalse(status["files_rollback_ok"])
        self.assertTrue(status["dependencies_may_have_changed"])
        self.assertTrue(status["recovery_required"])
        self.assertTrue(status["errors"])
        recovery = json.loads((root / "data" / "update_recovery.json").read_text(encoding="utf-8"))
        self.assertFalse(recovery["files_rollback_ok"])
        self.assertTrue(recovery["dependencies_may_have_changed"])
        self.assertTrue(recovery["recovery_required"])
        self.assertIn("rollback de archivos quedó incompleto", recovery["message"])

    def test_successful_pip_then_validation_failure_marks_dependency_uncertainty(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        before = snapshot_tree(root)
        with self.patch_install(root, managed), \
             mock.patch.object(nova_updater, "_install_requirements", return_value=None) as install, \
             mock.patch.object(nova_updater, "validate_install", return_value=(False, "post-pip validation failed")):
            with self.assertRaisesRegex(RuntimeError, "entorno Python puede haber cambiado"):
                nova_updater.execute_transaction(stage, list(new), set(original), "v-new", "old", "new", backup_root=backups)
        install.assert_called_once_with()
        self.assert_previous_tree_preserved_with_recovery_marker(root, before)
        status = json.loads((self.latest_backup(backups) / "rollback_status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["files_rollback_ok"])
        self.assertTrue(status["dependencies_may_have_changed"])
        self.assertTrue(status["recovery_required"])

    def test_rollback_without_requirements_change_never_marks_dependency_uncertainty(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        with self.patch_install(root, managed), \
             mock.patch.object(nova_updater, "_install_requirements") as install, \
             mock.patch.object(nova_updater, "validate_install", return_value=(False, "validation failed")):
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                nova_updater.execute_transaction(stage, list(new), set(original), "v-new", "old", "new", backup_root=backups)
        install.assert_not_called()
        status = json.loads((self.latest_backup(backups) / "rollback_status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["files_rollback_ok"])
        self.assertFalse(status["dependencies_may_have_changed"])
        self.assertFalse(status["recovery_required"])
        self.assertFalse((root / "data" / "update_recovery.json").exists())

    def test_restore_failure_is_explicit_and_backup_is_preserved(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        with self.patch_install(root, managed):
            manifest = nova_updater.build_transaction(stage, list(new), set(original))
            backup = nova_updater.create_backup(manifest, "old", "new", backup_root=backups)
            nova_updater.apply_transaction(stage, manifest)
            (backup / "files" / "assistant" / "modified.py").unlink()
            with self.assertRaisesRegex(RuntimeError, "rollback incompleto"):
                nova_updater.restore_backup(backup)
            self.assertTrue(backup.is_dir())
            status = json.loads((backup / "rollback_status.json").read_text(encoding="utf-8"))
            self.assertFalse(status["ok"])
            self.assertFalse(status["files_rollback_ok"])
            self.assertTrue(status["recovery_required"])
            self.assertTrue(status["errors"])
            failure = json.loads((root / "data" / "update_rollback_failure.json").read_text(encoding="utf-8"))
            self.assertFalse(failure["ok"])
            self.assertEqual((root / "assistant/unchanged.py").read_bytes(), original["assistant/unchanged.py"])

    def test_unsafe_manifest_path_is_rejected(self):
        _base, root, _stage, backups, managed, _original, _new = self.fixture()
        manifest = {name: [] for name in nova_updater.TRANSACTION_CATEGORIES}
        manifest["created_new"] = ["../outside.txt"]
        with self.patch_install(root, managed):
            with self.assertRaisesRegex(RuntimeError, "Ruta insegura"):
                nova_updater.create_backup(manifest, "old", "new", backup_root=backups)


if __name__ == "__main__":
    unittest.main()
