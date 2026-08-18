from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
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
        with tempfile.TemporaryDirectory() as td:
            result = HARNESS.run_disposable_file_fixture(ROOT, Path(td))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["detail"], "disposable_fixture_only")
        self.assertEqual(result["pending_requests"], 0)

    def test_windows_10_is_rejected_and_windows_11_build_is_accepted(self):
        self.assertFalse(HARNESS.is_windows_11({"system": "Windows", "build": 19045}))
        self.assertTrue(HARNESS.is_windows_11({"system": "Windows", "build": 22000}))
        self.assertTrue(HARNESS.is_windows_11({"system": "Windows", "build": 26100}))
        self.assertFalse(HARNESS.is_windows_11({"system": "Linux", "build": 26100}))

    def test_windows_extended_path_prefix_matches_regular_cmdline_form(self):
        self.assertEqual(HARNESS._normal_path(r"\\?\D:\a\fixture"), "d:/a/fixture")
        self.assertEqual(HARNESS._normal_path(r"D:\a\fixture"), "d:/a/fixture")

    def test_manual_checkpoints_are_exact_and_have_verifiable_preconditions(self):
        self.assertEqual(len(HARNESS.MANUAL_CHECKS), 13)
        self.assertEqual(
            {row["id"] for row in HARNESS.MANUAL_CHECKS},
            {"allow_once", "deny", "close_denies", "timeout", "grant_scope", "high_risk_reprompts",
             "neutral_submit", "toctou", "stop_shutdown", "same_task_resume", "hidden_notification",
             "repository_online_offline", "no_orphans"},
        )
        for row in HARNESS.MANUAL_CHECKS:
            self.assertTrue(row["precondition"])
            self.assertTrue(row["steps"])
            self.assertTrue(row["expected"])

    def test_process_scope_detects_fixture_helpers_and_ignores_unrelated_python(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td)
            owner = fixture / "install" / "nova" / "data" / "runtime" / "owner.json"
            owner.parent.mkdir(parents=True)
            owner.write_text(json.dumps({
                "pid": 20, "process_creation_time": 2, "owner_id": "a" * 32,
                "scope_id": "fixture", "session_id": 1, "role": "runtime",
            }), encoding="utf-8")
            runtime = str(fixture / "install" / "nova" / "app.py")
            rows = [
                {"pid": 10, "ppid": 1, "name": "python.exe", "exe": "C:/Python/python.exe", "cmdline": ["python.exe", "unrelated.py"], "create_time": 1.0, "thread_count": 2},
                {"pid": 20, "ppid": 1, "name": "python.exe", "exe": "C:/Python/python.exe", "cmdline": ["python.exe", runtime], "create_time": 2.0, "thread_count": 4},
                {"pid": 21, "ppid": 20, "name": "python.exe", "exe": "C:/Python/python.exe", "cmdline": ["python.exe", "handoff_helper.py"], "create_time": 3.0, "thread_count": 1},
            ]
            snapshot = HARNESS.sanitized_runtime_snapshot(
                fixture, process_source=rows, thread_source=lambda: [], window_source=lambda: [10, 20, 21],
            )
            self.assertEqual(snapshot["related_process_count"], 2)
            self.assertEqual(snapshot["roles"]["runtime"], 1)
            self.assertEqual(snapshot["roles"]["helper"], 1)
            self.assertEqual(snapshot["related_thread_count"], 5)
            self.assertEqual(snapshot["related_window_count"], 2)
            self.assertTrue(snapshot["owner_identity"]["complete"])
            unrelated = HARNESS.sanitized_runtime_snapshot(
                fixture, process_source=rows[:1], thread_source=lambda: [], window_source=lambda: [10],
            )
            self.assertEqual(unrelated["related_process_count"], 0)

            status, detail = HARNESS.orphan_check(unrelated, snapshot, pending_requests=0)
            self.assertEqual(status, "FAIL")
            self.assertIn("roles_clean=False", detail)

    def test_focal_failure_is_useful_and_redacted(self):
        secret = "NOVA-VERY-PRIVATE-TOKEN"
        private_path = r"C:\Users\private-user\Nova\secret.txt"
        def runner(*_args, **_kwargs):
            return SimpleNamespace(returncode=7, stdout=f"token={secret}", stderr=f"failure at {private_path}")
        result = HARNESS.run_focal(ROOT, runner=runner, extra_secrets=[secret])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["returncode"], 7)
        self.assertGreaterEqual(result["duration_seconds"], 0)
        self.assertEqual(result["suites"], HARNESS.FOCAL)
        self.assertNotIn(secret, result["error_excerpt"])
        self.assertNotIn("private-user", result["error_excerpt"])
        self.assertIn("failure", result["error_excerpt"])

    def test_evidence_zip_is_machine_readable_and_contains_no_secret(self):
        secret = "NOVA-SECRET-VALUE-123"
        evidence = {
            "schema": 2, "checks": [{"id": "safe", "status": "PASS", "required": True, "detail": secret}],
            "runtime_before": {"thread_count": 1}, "runtime_after": {"thread_count": 1},
            "token": secret, "diagnostic": f"password={secret} C:\\Users\\private-user\\Nova\\file.txt",
        }
        with tempfile.TemporaryDirectory() as td:
            json_path, zip_path = HARNESS.write_evidence(Path(td), evidence, extra_secrets=[secret])
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["schema"], 2)
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(set(archive.namelist()), {"nova-v010-validation.json", "summary.txt"})
                combined = b"".join(archive.read(name) for name in archive.namelist())
            self.assertNotIn(secret.encode(), combined)
            self.assertNotIn(b"private-user", combined)
            self.assertIn(b"[REDACTED]", combined)


if __name__ == "__main__":
    unittest.main()
