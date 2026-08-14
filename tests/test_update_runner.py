from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from updater.update_runner import (
    ShutdownCoordination,
    console_python,
    coordinate_runtime_shutdown,
    launch_nova,
    read_version,
    status_path,
    write_status,
)


class _SharedLock:
    def __init__(self, *, holder="runtime", role="runtime", creation_time=111):
        self.holder = holder
        self.owner = {
            "pid": 4242,
            "owner_id": "a" * 32,
            "role": role,
            "process_creation_time": creation_time,
        }


class _Lock:
    def __init__(self, shared, name="guard"):
        self.shared = shared
        self.name = name
        self.owned = False
    def acquire(self):
        if self.shared.holder is not None:
            return False
        self.shared.holder = self.name
        self.owned = True
        return True
    def release(self):
        if self.owned and self.shared.holder == self.name:
            self.shared.holder = None
            self.owned = False
    def read_owner(self):
        return dict(self.shared.owner) if self.shared.owner is not None else None


class _Mailbox:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []
    def send(self, command, *, target_owner_id=None):
        self.calls.append((command, target_owner_id))
        return self.ok


class _Process:
    def __init__(self, shared, *, terminates=True, creation_time=111, already_terminated=False, next_holder=None):
        self.shared = shared
        self.terminates = terminates
        self.creation_time = creation_time
        self.already_terminated = already_terminated
        self.next_holder = next_holder
        self.closed = False
        self.wait_calls = 0
    def matches_creation_time(self, expected):
        return bool(expected) and int(expected) == int(self.creation_time)
    def wait(self, _timeout):
        self.wait_calls += 1
        if self.already_terminated:
            return True
        if self.terminates:
            if self.shared.holder in {"runtime", "updater"}:
                self.shared.holder = self.next_holder
            return True
        return False
    def close(self):
        self.closed = True


class UpdateRunnerTests(unittest.TestCase):
    def lock_factory(self, shared):
        return lambda: _Lock(shared, "guard")

    def test_version_and_status_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "NOVA_VERSION.txt").write_text("0.6.2\n", encoding="utf-8")
            self.assertEqual(read_version(root), "0.6.2")
            log = root / "data" / "updater_logs" / "test.log"
            log.parent.mkdir(parents=True)
            log.write_text("ok", encoding="utf-8")
            write_status(root, ok=True, before="0.6.1", after="0.6.2", log=log)
            data = json.loads(status_path(root).read_text(encoding="utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["before"], "0.6.1")
            self.assertEqual(data["after"], "0.6.2")

    def test_console_python_returns_a_path(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(str(console_python(Path(td))))

    def test_runtime_real_with_occupied_lock_requires_process_exit_then_guard(self):
        shared = _SharedLock(holder="runtime")
        mailbox = _Mailbox()
        process = _Process(shared, terminates=True)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.5,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.process_terminated)
        self.assertTrue(result.lock_acquired)
        self.assertEqual(mailbox.calls, [("shutdown_for_update", "a" * 32)])
        self.assertTrue(process.closed)
        self.assertEqual(shared.holder, "guard")
        result.release_guard()
        self.assertIsNone(shared.holder)

    def test_free_lock_keeps_guard_while_matching_recorded_process_is_still_alive(self):
        shared = _SharedLock(holder=None)
        mailbox = _Mailbox()
        process = _Process(shared, terminates=False)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.01,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "owner_process_timeout")
        self.assertEqual(mailbox.calls, [])
        self.assertIsNone(shared.holder, "failed coordination must release its guard")

    def test_same_pid_different_creation_time_is_treated_as_stale_without_wait_or_command(self):
        shared = _SharedLock(holder=None, creation_time=111)
        mailbox = _Mailbox()
        process = _Process(shared, terminates=False, creation_time=222)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertTrue(result.ok)
        self.assertEqual(process.wait_calls, 0)
        self.assertEqual(mailbox.calls, [])
        self.assertTrue(result.lock_acquired)
        result.release_guard()

    def test_stale_runtime_metadata_with_free_lock_is_recovered(self):
        shared = _SharedLock(holder=None, role="runtime")
        process = _Process(shared, already_terminated=True)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=self.lock_factory(shared),
            mailbox=_Mailbox(),
            process_factory=lambda pid: process,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.owner_role, "runtime")
        self.assertTrue(result.lock_acquired)
        result.release_guard()

    def test_stale_updater_metadata_after_crash_is_recovered(self):
        shared = _SharedLock(holder=None, role="updater")
        process = _Process(shared, already_terminated=True)
        mailbox = _Mailbox()
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertTrue(result.ok)
        self.assertEqual(mailbox.calls, [])
        self.assertEqual(shared.holder, "guard")
        result.release_guard()

    def test_real_updater_still_active_is_waited_without_shutdown_command(self):
        shared = _SharedLock(holder="updater", role="updater")
        mailbox = _Mailbox()
        process = _Process(shared, terminates=True)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.5,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertTrue(result.ok)
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertEqual(mailbox.calls, [])
        self.assertEqual(shared.holder, "guard")
        result.release_guard()

    def test_other_runtime_wins_guard_race_after_old_process_exit_and_update_fails_safe(self):
        shared = _SharedLock(holder="runtime")
        mailbox = _Mailbox()
        process = _Process(shared, terminates=True, next_holder="competitor")
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.05,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "runtime_lock_timeout_after_process_exit")
        self.assertEqual(shared.holder, "competitor")
        self.assertEqual(mailbox.calls, [("shutdown_for_update", "a" * 32)])

    def test_legacy_metadata_without_creation_time_is_safe_when_lock_free(self):
        shared = _SharedLock(holder=None)
        shared.owner.pop("process_creation_time")
        called = []
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=self.lock_factory(shared),
            mailbox=_Mailbox(),
            process_factory=lambda pid: called.append(pid),
        )
        self.assertTrue(result.ok)
        self.assertEqual(called, [], "legacy PID must not be waited without a creation identity")
        self.assertTrue(result.lock_acquired)
        result.release_guard()

    def test_legacy_metadata_without_creation_time_fails_closed_when_lock_occupied(self):
        shared = _SharedLock(holder="runtime")
        shared.owner.pop("process_creation_time")
        mailbox = _Mailbox()
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.02,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: self.fail("legacy occupied PID must not be captured"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "runtime_owner_identity_unavailable")
        self.assertEqual(mailbox.calls, [])

    def test_failed_shutdown_delivery_never_authorizes_update(self):
        shared = _SharedLock(holder="runtime")
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=self.lock_factory(shared),
            mailbox=_Mailbox(ok=False),
            process_factory=lambda pid: _Process(shared),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "shutdown_command_delivery_failed")

    def test_post_update_relaunches_exactly_one_visible_instance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("# test\n", encoding="utf-8")
            with patch("updater.update_runner.subprocess.Popen") as popen:
                ok, _detail = launch_nova(root)
            self.assertTrue(ok)
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[-1], "--post-update")
            self.assertNotIn("--background", command)

    def test_main_does_not_run_updater_when_runtime_termination_is_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("updater.update_runner.nova_root", return_value=root), \
                 patch("updater.update_runner.coordinate_runtime_shutdown", return_value=ShutdownCoordination(False, "owner_process_timeout", 7, "a" * 32)), \
                 patch("updater.update_runner.run_update") as run_update, \
                 patch("updater.update_runner._show_surviving_runtime", return_value=True):
                from updater import update_runner
                rc = update_runner.main(["--wait-seconds", "0.01"])
            self.assertEqual(rc, 4)
            run_update.assert_not_called()

    def test_updater_failure_still_relaunches_once_after_verified_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("updater.update_runner.nova_root", return_value=root), \
                 patch("updater.update_runner.coordinate_runtime_shutdown", return_value=ShutdownCoordination(True, process_terminated=True, lock_acquired=True)), \
                 patch("updater.update_runner.run_update", return_value=(2, "failed")), \
                 patch("updater.update_runner.launch_nova", return_value=(True, "ok")) as launch:
                from updater import update_runner
                rc = update_runner.main([])
            self.assertEqual(rc, 2)
            launch.assert_called_once_with(root)


if __name__ == "__main__":
    unittest.main()
