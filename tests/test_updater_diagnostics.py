from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from assistant.doctor_updater import updater_diagnostic
from assistant.instance_lock import runtime_paths
from assistant.update_supervisor import SUPERVISOR_LOCK_FILENAME, supervisor_mutex_path


class UpdaterDiagnosticTests(unittest.TestCase):
    def test_supervisor_mutex_is_separate_file_inside_runtime_user_session_scope(self):
        paths = runtime_paths()
        mutex = supervisor_mutex_path()
        self.assertEqual(mutex.parent, paths.directory)
        self.assertEqual(mutex.name, SUPERVISOR_LOCK_FILENAME)
        self.assertNotEqual(mutex, paths.lock)

    def test_doctor_reports_active_supervisor_last_update_and_unconfirmed_pip_pids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            (data / "update_last.json").write_text(
                json.dumps({
                    "ok": False,
                    "before": "0.9.8",
                    "after": "0.9.8",
                    "state": "pip_termination_unconfirmed",
                    "error": "recovery required",
                    "arguments": "--token SHOULD_NOT_APPEAR",
                }),
                encoding="utf-8",
            )
            (data / "update_recovery.json").write_text(
                json.dumps({
                    "status": "pip_termination_unconfirmed",
                    "recovery_required": True,
                    "remaining_pids": [123, 456],
                    "command_line": "secret external path --token abc",
                }),
                encoding="utf-8",
            )
            result = updater_diagnostic(root, supervisor_probe=lambda: {
                "active": True,
                "ownership": "kernel_file_lock",
                "scope": "user_session",
                "error": "",
            })
        self.assertEqual(result["status"], "warn")
        self.assertIn("supervisor activo", result["detail"])
        self.assertIn("recuperación pendiente", result["detail"])
        self.assertIn("terminación de pip no confirmada", result["detail"])
        self.assertIn("123, 456", result["detail"])
        self.assertNotIn("SHOULD_NOT_APPEAR", result["detail"])
        self.assertNotIn("secret external path", result["detail"])
        self.assertTrue(result["updater"]["pip_termination_unconfirmed"])
        self.assertEqual(result["updater"]["remaining_pids"], [123, 456])

    def test_doctor_hides_irrelevant_remaining_pids_when_recovery_is_not_pip_unconfirmed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            (data / "update_recovery.json").write_text(
                json.dumps({
                    "status": "rollback_completed_dependency_recovery",
                    "recovery_required": True,
                    "remaining_pids": [999],
                }),
                encoding="utf-8",
            )
            result = updater_diagnostic(root, supervisor_probe=lambda: {
                "active": False,
                "ownership": "kernel_file_lock",
                "scope": "user_session",
                "error": "",
            })
        self.assertIn("supervisor inactivo", result["detail"])
        self.assertIn("recuperación pendiente", result["detail"])
        self.assertNotIn("999", result["detail"])
        self.assertFalse(result["updater"]["pip_termination_unconfirmed"])
        self.assertEqual(result["updater"]["remaining_pids"], [])


if __name__ == "__main__":
    unittest.main()
