from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("validate_v010_windows", ROOT / "tools" / "validate_v010_windows.py")
HARNESS = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(HARNESS)


class WindowsHarnessTests(unittest.TestCase):
    def test_required_not_run_and_fail_are_nonzero(self):
        self.assertEqual(HARNESS.evidence_exit_code([{"required": True, "status": "PASS"}]), 0)
        self.assertNotEqual(HARNESS.evidence_exit_code([{"required": True, "status": "NOT_RUN"}]), 0)
        self.assertNotEqual(HARNESS.evidence_exit_code([{"required": True, "status": "FAIL"}]), 0)

    def test_disposable_fixture_executes_once_and_cleans_up(self):
        status, detail = HARNESS.run_disposable_file_fixture(ROOT)
        self.assertEqual((status, detail), ("PASS", "disposable_fixture_only"))

    def test_evidence_zip_is_machine_readable_and_contains_no_secret(self):
        evidence = {
            "schema": 1, "checks": [{"id": "safe", "status": "PASS", "required": True}],
            "runtime_before": {"thread_count": 1}, "runtime_after": {"thread_count": 1},
        }
        with tempfile.TemporaryDirectory() as td:
            json_path, zip_path = HARNESS.write_evidence(Path(td), evidence)
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["schema"], 1)
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(set(archive.namelist()), {"nova-v010-validation.json", "summary.txt"})
                combined = b"".join(archive.read(name) for name in archive.namelist())
            self.assertNotIn(b"password", combined.lower())
            self.assertNotIn(b"token", combined.lower())


if __name__ == "__main__":
    unittest.main()
