from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from updater.pip_safety import (
    PipContainmentSetupError,
    WindowsJobApi,
    WindowsJobProcess,
    verify_normal_completion,
)


class _WinError(OSError):
    def __init__(self, code, message="win32 failure"):
        super().__init__(message)
        self.winerror = int(code)


class FakeJobApi:
    ERROR_ACCESS_DENIED = 5

    def __init__(self, *, fail_configure=False, fail_assign=False, breakaway_denied=False, fail_resume=False):
        self.events = []
        self.fail_configure = fail_configure
        self.fail_assign = fail_assign
        self.breakaway_denied = breakaway_denied
        self.fail_resume = fail_resume
        self.closed = []
        self.process = object()
        self.thread = object()
        self.job = object()
        self._create_calls = 0

    def create_job(self):
        self.events.append("create_job")
        return self.job

    def configure_kill_on_close(self, job):
        self.events.append(("configure", WindowsJobApi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE))
        if self.fail_configure:
            raise OSError("configure failed")

    def create_suspended(self, command, cwd, *, breakaway):
        self._create_calls += 1
        self.events.append(("create_suspended", bool(breakaway)))
        if breakaway and self.breakaway_denied:
            raise _WinError(self.ERROR_ACCESS_DENIED)
        return SimpleNamespace(
            hProcess=self.process,
            hThread=self.thread,
            dwProcessId=4321,
            dwThreadId=9876,
        )

    def assign(self, job, process):
        self.events.append("assign")
        if self.fail_assign:
            raise OSError("assign failed")

    def resume(self, thread):
        self.events.append("resume")
        if self.fail_resume:
            raise OSError("resume failed")

    def terminate_process(self, process, code=1):
        self.events.append("terminate_process")

    def terminate_job(self, job, code=1):
        self.events.append("terminate_job")

    def wait_process(self, process, timeout):
        self.events.append("wait_process")
        return True

    def close_handle(self, handle):
        label = "job" if handle is self.job else ("process" if handle is self.process else "thread")
        self.closed.append(label)
        self.events.append(("close", label))

    def exit_code(self, process):
        return 0

    def active_process_count(self, job):
        return 0

    def process_ids(self, job):
        return []


class JobObjectUnitTests(unittest.TestCase):
    def test_kill_on_close_flag_is_exact(self):
        self.assertEqual(WindowsJobApi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0x00002000)

    def test_process_is_assigned_before_resume(self):
        api = FakeJobApi()
        proc = WindowsJobProcess.launch(["python", "-m", "pip"], cwd=".", api=api)
        try:
            self.assertLess(api.events.index("assign"), api.events.index("resume"))
            self.assertLess(
                next(i for i, row in enumerate(api.events) if isinstance(row, tuple) and row[0] == "configure"),
                next(i for i, row in enumerate(api.events) if isinstance(row, tuple) and row[0] == "create_suspended"),
            )
            self.assertTrue(proc.authoritative_containment)
        finally:
            proc.close()
        self.assertIn("thread", api.closed)
        self.assertIn("process", api.closed)
        self.assertIn("job", api.closed)

    def test_configuration_failure_occurs_before_process_creation(self):
        api = FakeJobApi(fail_configure=True)
        with self.assertRaises(PipContainmentSetupError) as ctx:
            WindowsJobProcess.launch(["python", "-m", "pip"], api=api)
        self.assertFalse(ctx.exception.dependency_started)
        self.assertEqual(api._create_calls, 0)
        self.assertNotIn("resume", api.events)
        self.assertEqual(api.closed.count("job"), 1)

    def test_assignment_failure_never_resumes_suspended_process(self):
        api = FakeJobApi(fail_assign=True)
        with self.assertRaises(PipContainmentSetupError) as ctx:
            WindowsJobProcess.launch(["python", "-m", "pip"], api=api)
        self.assertFalse(ctx.exception.dependency_started)
        self.assertIn("assign", api.events)
        self.assertNotIn("resume", api.events)
        self.assertIn("terminate_process", api.events)
        self.assertIn("wait_process", api.events)
        self.assertEqual(api.closed.count("thread"), 1)
        self.assertEqual(api.closed.count("process"), 1)
        self.assertEqual(api.closed.count("job"), 1)

    def test_nested_job_breakaway_denial_retries_still_suspended(self):
        api = FakeJobApi(breakaway_denied=True)
        proc = WindowsJobProcess.launch(["python", "-m", "pip"], api=api)
        try:
            creates = [row for row in api.events if isinstance(row, tuple) and row[0] == "create_suspended"]
            self.assertEqual(creates, [("create_suspended", True), ("create_suspended", False)])
            self.assertLess(api.events.index("assign"), api.events.index("resume"))
        finally:
            proc.close()

    def test_resume_failure_terminates_job_and_cleans_all_handles(self):
        api = FakeJobApi(fail_resume=True)
        with self.assertRaises(PipContainmentSetupError):
            WindowsJobProcess.launch(["python", "-m", "pip"], api=api)
        self.assertIn("assign", api.events)
        self.assertIn("resume", api.events)
        self.assertIn("terminate_job", api.events)
        self.assertEqual(api.closed.count("thread"), 1)
        self.assertEqual(api.closed.count("process"), 1)
        self.assertEqual(api.closed.count("job"), 1)


@unittest.skipUnless(os.name == "nt", "real Windows Job Object integration")
class JobObjectWindowsIntegrationTests(unittest.TestCase):
    def _wait_path(self, path: Path, timeout=5.0):
        deadline = time.monotonic() + float(timeout)
        event = threading.Event()
        while time.monotonic() < deadline:
            if path.exists():
                return True
            event.wait(0.02)
        return path.exists()

    def _wait_identity_gone_or_reused(self, api: WindowsJobApi, pid: int, creation: int, timeout=5.0):
        deadline = time.monotonic() + float(timeout)
        event = threading.Event()
        while time.monotonic() < deadline:
            try:
                current = api.process_creation_time(pid)
            except OSError:
                current = None
            if current is None or int(current) != int(creation):
                return True
            event.wait(0.02)
        return False

    def _launch_tree(self, root: Path, *, root_exits=False):
        ready = root / "child.ready"
        pid_file = root / "child.pid"
        started = root / "root.started"
        # child.ready is deliberately the LAST write: its existence is the
        # synchronization barrier proving child.pid is already durable enough
        # to read. No arbitrary sleep is used as the correctness mechanism.
        child_code = (
            "from pathlib import Path; import os, threading; "
            f"Path(r'{pid_file}').write_text(str(os.getpid()), encoding='utf-8'); "
            f"Path(r'{ready}').write_text('ready', encoding='utf-8'); "
            "threading.Event().wait(30)"
        )
        root_code = (
            "from pathlib import Path; import subprocess, sys, threading, time; "
            f"Path(r'{started}').write_text('started', encoding='utf-8'); "
            f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
            f"deadline=time.monotonic()+5; e=threading.Event(); "
            f"ready=Path(r'{ready}'); "
            "\nwhile time.monotonic()<deadline and not ready.exists(): e.wait(0.01)\n"
            + ("raise SystemExit(0)" if root_exits else "threading.Event().wait(30)")
        )
        try:
            proc = WindowsJobProcess.launch([sys.executable, "-c", root_code], cwd=str(root))
        except PipContainmentSetupError as exc:
            # A CI host may forbid all nested-job assignment. It is acceptable
            # only if the suspended process never executed user/pip code.
            self.assertFalse(started.exists(), "containment setup failed after process execution")
            self.skipTest(f"runner does not permit nested Job assignment: {exc}")
        try:
            self.assertTrue(self._wait_path(ready), "contained child did not signal readiness")
            self.assertTrue(pid_file.is_file(), "ready signal appeared before child PID metadata")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            api = proc.api
            creation = api.process_creation_time(child_pid)
            self.assertIsNotNone(creation)
            return proc, child_pid, int(creation)
        except BaseException:
            # Do not leak a contained process or keep the TemporaryDirectory in
            # use when a test assertion/setup step itself fails.
            try:
                proc.close()
            finally:
                raise

    def test_closing_kill_on_close_job_terminates_root_and_descendant(self):
        with tempfile.TemporaryDirectory() as td:
            proc, child_pid, creation = self._launch_tree(Path(td), root_exits=False)
            api = proc.api
            proc.close()
            self.assertTrue(
                self._wait_identity_gone_or_reused(api, child_pid, creation),
                "child survived closing JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE container",
            )

    def test_fast_root_exit_cannot_hide_surviving_child(self):
        with tempfile.TemporaryDirectory() as td:
            proc, child_pid, creation = self._launch_tree(Path(td), root_exits=True)
            api = proc.api
            try:
                self.assertEqual(proc.wait(timeout=5), 0)
                # The direct/root process is gone while its child is intentionally
                # alive. A root-only/snapshot implementation can false-positive here.
                self.assertEqual(api.process_creation_time(child_pid), creation)
                result = verify_normal_completion(proc, 1.0)
                self.assertIsNotNone(result)
                self.assertTrue(result.authoritative_containment)
                self.assertTrue(result.terminated_confirmed, result.detail + " " + repr(result.termination_errors))
                self.assertTrue(result.rollback_allowed)
                self.assertFalse(result.remaining_processes)
                self.assertTrue(
                    self._wait_identity_gone_or_reused(api, child_pid, creation),
                    "orphan child survived an authoritative Job completion check",
                )
            finally:
                try:
                    proc.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
