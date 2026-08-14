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
    def __init__(self):
        self.owner_locked = True
        self.owner = {"pid": 4242, "owner_id": "a" * 32}


class _Lock:
    def __init__(self, shared):
        self.shared = shared
        self.owned = False
    def acquire(self):
        if self.shared.owner_locked:
            return False
        self.shared.owner_locked = True
        self.owned = True
        return True
    def release(self):
        if self.owned:
            self.shared.owner_locked = False
            self.owned = False
    def read_owner(self):
        return dict(self.shared.owner)


class _Mailbox:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []
    def send(self, command, *, target_owner_id=None):
        self.calls.append((command, target_owner_id))
        return self.ok


class _Process:
    def __init__(self, shared, terminates=True):
        self.shared = shared
        self.terminates = terminates
        self.already_terminated = False
        self.closed = False
    def wait(self, _timeout):
        if self.terminates:
            self.shared.owner_locked = False
            return True
        return False
    def close(self):
        self.closed = True


class UpdateRunnerTests(unittest.TestCase):
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

    def test_update_requires_process_exit_and_lock_reacquisition(self):
        shared = _SharedLock()
        mailbox = _Mailbox()
        process = _Process(shared, terminates=True)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.5,
            lock_factory=lambda: _Lock(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.process_terminated)
        self.assertTrue(result.lock_acquired)
        self.assertEqual(mailbox.calls, [("shutdown_for_update", "a" * 32)])
        self.assertTrue(process.closed)

    def test_free_lock_does_not_authorize_update_while_recorded_owner_pid_is_alive(self):
        shared = _SharedLock()
        shared.owner_locked = False
        mailbox = _Mailbox()
        process = _Process(shared, terminates=False)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.01,
            lock_factory=lambda: _Lock(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "owner_process_timeout")
        self.assertEqual(mailbox.calls, [])

    def test_failed_shutdown_delivery_never_authorizes_update(self):
        shared = _SharedLock()
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=lambda: _Lock(shared),
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
