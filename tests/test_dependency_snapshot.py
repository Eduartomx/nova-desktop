from __future__ import annotations

import json
from pathlib import Path
import tempfile
import types
import unittest

from updater.recovery_state import (
    capture_dependency_snapshot,
    create_transaction_journal,
    transition_journal,
    validate_restored_install,
)


class FakeDist:
    def __init__(self, name: str, version: str):
        self.metadata = {"Name": name}
        self.version = version


class DependencySnapshotTests(unittest.TestCase):
    def _fixture(self, td: str):
        root = Path(td) / "nova"
        (root / "updater").mkdir(parents=True)
        (root / "data" / "updater_backups").mkdir(parents=True)
        (root / "app.py").write_text("VALUE=1\n", encoding="utf-8")
        (root / "updater" / "nova_updater.py").write_text("VALUE=1\n", encoding="utf-8")
        (root / "updater" / "update_runner.py").write_text("VALUE=1\n", encoding="utf-8")
        (root / "updater" / "managed_files.json").write_text('{"files":["app.py"]}', encoding="utf-8")
        (root / "requirements.txt").write_text("Alpha\nBeta\n", encoding="utf-8")
        backup = root / "data" / "updater_backups" / "attempt"
        (backup / "control").mkdir(parents=True)
        (backup / "files").mkdir()
        (backup / "control" / "managed_files.json").write_text('{"files":["app.py"]}', encoding="utf-8")
        (backup / "backup.json").write_text(json.dumps({
            "schema": 2, "from": "old", "to": "new",
            "modified_existing": [], "deleted_existing": [], "created_new": [], "unchanged": ["app.py"],
            "managed_files": {"path": "updater/managed_files.json", "existed": True, "backup": "control/managed_files.json"},
        }), encoding="utf-8")
        old = [FakeDist("Requests", "1.0"), FakeDist("Beta", "2.0")]
        rel, sha = capture_dependency_snapshot(root, backup, distributions=old)
        journal = create_transaction_journal(
            root, backup, dependency_snapshot_path=rel, dependency_snapshot_sha256=sha
        )
        journal = transition_journal(root, journal, "files_applying", files_may_have_changed=True)
        journal = transition_journal(root, journal, "files_applied")
        journal = transition_journal(root, journal, "dependencies_starting", dependencies_may_have_changed=True)
        journal = transition_journal(root, journal, "rollback_in_progress")
        journal = transition_journal(root, journal, "rollback_completed")
        journal = transition_journal(root, journal, "rollback_validation_in_progress")
        return root, backup, journal, old

    @staticmethod
    def _imports_ok(*_args, **_kwargs):
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def test_same_versions_and_set_validate(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, old = self._fixture(td)
            ok, detail = validate_restored_install(
                root, journal, backup, distributions=old, import_runner=self._imports_ok
            )
            self.assertTrue(ok, detail)

    def test_removed_distribution_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, _old = self._fixture(td)
            ok, detail = validate_restored_install(
                root, journal, backup, distributions=[FakeDist("Requests", "1.0")], import_runner=self._imports_ok
            )
            self.assertFalse(ok)
            self.assertTrue(detail.startswith("dependency_removed:"), detail)

    def test_added_distribution_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, old = self._fixture(td)
            current = old + [FakeDist("Gamma", "3.0")]
            ok, detail = validate_restored_install(root, journal, backup, distributions=current, import_runner=self._imports_ok)
            self.assertFalse(ok)
            self.assertTrue(detail.startswith("dependency_added:"), detail)

    def test_changed_version_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, _old = self._fixture(td)
            current = [FakeDist("Requests", "1.1"), FakeDist("Beta", "2.0")]
            ok, detail = validate_restored_install(root, journal, backup, distributions=current, import_runner=self._imports_ok)
            self.assertFalse(ok)
            self.assertTrue(detail.startswith("dependency_version_changed:"), detail)

    def test_snapshot_corruption_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, old = self._fixture(td)
            (backup / journal["dependency_snapshot_path"]).write_text("{broken", encoding="utf-8")
            ok, detail = validate_restored_install(root, journal, backup, distributions=old, import_runner=self._imports_ok)
            self.assertFalse(ok)
            self.assertIn("dependency_snapshot_hash_mismatch", detail)

    def test_wrong_snapshot_hash_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, old = self._fixture(td)
            changed = dict(journal)
            changed["dependency_snapshot_sha256"] = "0" * 64
            ok, detail = validate_restored_install(root, changed, backup, distributions=old, import_runner=self._imports_ok)
            self.assertFalse(ok)
            self.assertIn("dependency_snapshot_hash_mismatch", detail)

    def test_restored_requirements_with_different_environment_stays_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, _old = self._fixture(td)
            self.assertEqual((root / "requirements.txt").read_text(encoding="utf-8"), "Alpha\nBeta\n")
            ok, detail = validate_restored_install(
                root, journal, backup,
                distributions=[FakeDist("Requests", "1.0"), FakeDist("Beta", "9.0")],
                import_runner=self._imports_ok,
            )
            self.assertFalse(ok)
            self.assertIn("dependency_version_changed", detail)

    def test_dependencies_not_started_do_not_require_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, _old = self._fixture(td)
            no_dep = dict(journal)
            no_dep["dependencies_may_have_changed"] = False
            no_dep["dependency_snapshot_path"] = ""
            no_dep["dependency_snapshot_sha256"] = ""
            ok, detail = validate_restored_install(root, no_dep, backup, distributions=[])
            self.assertTrue(ok, detail)

    def test_critical_import_failure_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, old = self._fixture(td)
            failed = lambda *_a, **_kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
            ok, detail = validate_restored_install(root, journal, backup, distributions=old, import_runner=failed)
            self.assertFalse(ok)
            self.assertEqual(detail, "critical_import_failed")

    def test_validation_never_invokes_pip(self):
        with tempfile.TemporaryDirectory() as td:
            root, backup, journal, old = self._fixture(td)
            calls = []
            def runner(command, **kwargs):
                calls.append(list(command))
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            ok, detail = validate_restored_install(root, journal, backup, distributions=old, import_runner=runner)
            self.assertTrue(ok, detail)
            self.assertTrue(calls)
            self.assertFalse(any("pip" in part.casefold() for command in calls for part in command))


if __name__ == "__main__":
    unittest.main()
