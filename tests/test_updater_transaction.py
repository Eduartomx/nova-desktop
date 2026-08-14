from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from updater import nova_updater


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

    def timed_out_process(self, timeout):
        proc = mock.Mock()
        proc.wait.side_effect = [subprocess.TimeoutExpired(["pip"], timeout), 0]
        return proc

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
            proc = mock.Mock()
            proc.wait.return_value = 0
            with mock.patch.object(nova_updater, "ROOT", root), \
                 mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc) as popen:
                nova_updater._install_requirements(timeout_seconds=12.5)
        popen.assert_called_once()
        self.assertNotIn("shell", popen.call_args.kwargs)
        proc.wait.assert_called_once_with(timeout=12.5)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_pip_timeout_validation_rejects_nonpositive_and_caps_absurd_values(self):
        with self.assertRaises(ValueError):
            nova_updater._normalize_pip_timeout(0)
        with self.assertRaises(ValueError):
            nova_updater._normalize_pip_timeout(-1)
        self.assertEqual(
            nova_updater._normalize_pip_timeout(nova_updater.PIP_INSTALL_TIMEOUT_MAX_SECONDS * 100),
            nova_updater.PIP_INSTALL_TIMEOUT_MAX_SECONDS,
        )

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

    def test_pip_timeout_restores_sha_removes_created_restores_managed_and_preserves_backup(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        before = snapshot_tree(root)
        managed_before = managed.read_bytes()
        timeout = 0.25
        proc = self.timed_out_process(timeout)
        with self.patch_install(root, managed), \
             mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc) as popen, \
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
        proc.terminate.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        self.assert_previous_tree_preserved_with_recovery_marker(root, before)
        self.assertFalse((root / "assistant" / "created.py").exists())
        self.assertEqual(managed.read_bytes(), managed_before)
        backup = self.latest_backup(backups)
        self.assertTrue(backup.is_dir())
        status = json.loads((backup / "rollback_status.json").read_text(encoding="utf-8"))
        self.assertTrue(status["files_rollback_ok"])
        self.assertTrue(status["dependencies_may_have_changed"])
        self.assertTrue(status["recovery_required"])
        self.assertIn("timeout", status["recovery_detail"])
        recovery_path = root / "data" / "update_recovery.json"
        self.assertTrue(recovery_path.is_file())
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        self.assertTrue(recovery["files_rollback_ok"])
        self.assertTrue(recovery["dependencies_may_have_changed"])
        self.assertTrue(recovery["recovery_required"])
        self.assertIn("timeout", recovery["message"])

    def test_pip_timeout_with_partial_rollback_marks_both_failures_and_preserves_backup(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        timeout = 0.25
        proc = self.timed_out_process(timeout)
        real_replace = nova_updater._atomic_replace_from

        def fail_one_restore(src, dst):
            src = Path(src)
            dst = Path(dst)
            if backups in src.parents and dst == root / "assistant" / "modified.py":
                raise RuntimeError("injected restore failure")
            return real_replace(src, dst)

        with self.patch_install(root, managed), \
             mock.patch("updater.nova_updater.subprocess.Popen", return_value=proc), \
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
        self.assertIn("timeout", status["recovery_detail"])
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
