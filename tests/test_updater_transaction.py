from __future__ import annotations

import hashlib
import json
from pathlib import Path
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

    def test_validation_failure_restores_tree_byte_for_byte(self):
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
            self.assertTrue((root / "assistant/created.py").is_file(), "failure must happen after a real created file was copied")
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

    def test_dependency_install_failure_is_rolled_back_without_running_pip(self):
        _base, root, stage, backups, managed, original, new = self.fixture()
        write_file(stage, "requirements.txt", b"package-new==2\n")
        before = snapshot_tree(root)
        with self.patch_install(root, managed), \
             mock.patch.object(nova_updater, "_install_requirements", side_effect=RuntimeError("simulated pip failure")) as install, \
             mock.patch.object(nova_updater, "validate_install") as validate:
            with self.assertRaisesRegex(RuntimeError, "simulated pip failure"):
                nova_updater.execute_transaction(stage, list(new), set(original), "v-new", "old", "new", backup_root=backups)
        install.assert_called_once_with()
        validate.assert_not_called()
        self.assertEqual(snapshot_tree(root), before)

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
