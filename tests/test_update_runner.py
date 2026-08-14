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


class _EventGuard:
    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail
        self.calls = 0
    def release(self):
        self.calls += 1
        self.events.append("release_guard")
        if self.fail:
            raise RuntimeError("release failed")


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
        self.assertTrue(result.process_terminated)
        self.assertEqual(shared.holder, "competitor")
        self.assertEqual(mailbox.calls, [("shutdown_for_update", "a" * 32)])

    def test_legacy_metadata_without_creation_time_is_recovered_only_when_pid_is_dead(self):
        shared = _SharedLock(holder=None)
        shared.owner.pop("process_creation_time")
        process = _Process(shared, already_terminated=True, creation_time=999)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=self.lock_factory(shared),
            mailbox=_Mailbox(),
            process_factory=lambda pid: process,
        )
        self.assertTrue(result.ok)
        self.assertEqual(process.wait_calls, 0)
        self.assertTrue(result.lock_acquired)
        result.release_guard()

    def test_legacy_metadata_with_free_lock_and_live_pid_fails_closed_without_waiting(self):
        shared = _SharedLock(holder=None)
        shared.owner.pop("process_creation_time")
        mailbox = _Mailbox()
        process = _Process(shared, terminates=True, creation_time=999)
        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.1,
            lock_factory=self.lock_factory(shared),
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "runtime_owner_identity_unavailable")
        self.assertEqual(process.wait_calls, 0)
        self.assertEqual(mailbox.calls, [])
        self.assertIsNone(shared.holder, "unverifiable live legacy PID must release updater guard")

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

    def test_lock_construction_error_is_structured_and_never_escapes(self):
        def broken_factory():
            raise OSError("lock construction failed")

        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.01,
            lock_factory=broken_factory,
            guard_factory=broken_factory,
            mailbox=_Mailbox(),
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.process_terminated)
        self.assertIn("coordination_exception:OSError", result.error)

    def test_guard_acquire_error_after_verified_runtime_exit_is_structured_and_shutdown_sent_once(self):
        shared = _SharedLock(holder="runtime")
        mailbox = _Mailbox()
        process = _Process(shared, terminates=True)
        calls = {"count": 0}

        def guard_factory():
            calls["count"] += 1
            if calls["count"] == 1:
                return _Lock(shared, "guard")
            raise OSError("guard reopen failed")

        result = coordinate_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.2,
            lock_factory=lambda: _Lock(shared, "observer"),
            guard_factory=guard_factory,
            mailbox=mailbox,
            process_factory=lambda pid: process,
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.process_terminated)
        self.assertIn("runtime_guard_acquire_failed:OSError", result.error)
        self.assertEqual(mailbox.calls, [("shutdown_for_update", "a" * 32)])

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

    def _run_main_case(
        self,
        *,
        update_result=(0, ""),
        update_exception=None,
        read_side_effect=None,
        status_exception=None,
        release_exception=False,
        launch_result=(True, "ok"),
        launch_exception=None,
    ):
        from updater import update_runner
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events = []
        guard = _EventGuard(events, fail=release_exception)
        coordination = ShutdownCoordination(True, process_terminated=True, lock_acquired=True, guard=guard)

        def fake_update(_root, _log):
            events.append("run_update")
            if update_exception is not None:
                raise update_exception
            return update_result

        def fake_launch(_root):
            events.append("launch_nova")
            if launch_exception is not None:
                raise launch_exception
            return launch_result

        read_value = read_side_effect if read_side_effect is not None else ["0.9.8", "0.9.9"]
        status_side_effect = status_exception if status_exception is not None else None
        with patch("updater.update_runner.nova_root", return_value=root), \
             patch("updater.update_runner.coordinate_runtime_shutdown", return_value=coordination), \
             patch("updater.update_runner.run_update", side_effect=fake_update), \
             patch("updater.update_runner.read_version", side_effect=read_value), \
             patch("updater.update_runner.write_status", side_effect=status_side_effect) as status, \
             patch("updater.update_runner.launch_nova", side_effect=fake_launch) as launch:
            rc = update_runner.main([])
        return rc, events, guard, launch, status

    def assert_update_release_launch_order(self, events):
        self.assertIn("run_update", events)
        self.assertIn("release_guard", events)
        self.assertIn("launch_nova", events)
        self.assertLess(events.index("run_update"), events.index("release_guard"))
        self.assertLess(events.index("release_guard"), events.index("launch_nova"))

    def test_success_releases_guard_before_exactly_one_launch(self):
        rc, events, guard, launch, _status = self._run_main_case()
        self.assertEqual(rc, 0)
        self.assert_update_release_launch_order(events)
        self.assertEqual(guard.calls, 1)
        launch.assert_called_once()

    def test_run_update_error_relaunches_exactly_once(self):
        rc, events, guard, launch, _status = self._run_main_case(update_result=(7, "failed"))
        self.assertEqual(rc, 7)
        self.assert_update_release_launch_order(events)
        self.assertEqual(guard.calls, 1)
        launch.assert_called_once()

    def test_pip_timeout_updater_error_keeps_primary_code_and_relaunches_once(self):
        rc, events, guard, launch, _status = self._run_main_case(
            update_result=(2, "pip install excedió el timeout configurado")
        )
        self.assertEqual(rc, 2)
        self.assert_update_release_launch_order(events)
        self.assertEqual(guard.calls, 1)
        launch.assert_called_once()

    def test_run_update_exception_relaunches_exactly_once(self):
        rc, events, _guard, launch, _status = self._run_main_case(update_exception=RuntimeError("boom"))
        self.assertEqual(rc, 2)
        self.assert_update_release_launch_order(events)
        launch.assert_called_once()

    def test_second_read_version_failure_does_not_block_launch(self):
        rc, events, _guard, launch, _status = self._run_main_case(
            read_side_effect=["0.9.8", RuntimeError("version read failed")]
        )
        self.assertEqual(rc, 0)
        self.assert_update_release_launch_order(events)
        launch.assert_called_once()

    def test_write_status_failure_after_success_does_not_block_launch(self):
        rc, events, _guard, launch, status = self._run_main_case(status_exception=RuntimeError("status failed"))
        self.assertEqual(rc, 0)
        self.assert_update_release_launch_order(events)
        status.assert_called_once()
        launch.assert_called_once()

    def test_write_status_failure_after_update_failure_does_not_block_launch(self):
        rc, events, _guard, launch, status = self._run_main_case(
            update_result=(9, "update failed"), status_exception=RuntimeError("status failed")
        )
        self.assertEqual(rc, 9)
        self.assert_update_release_launch_order(events)
        status.assert_called_once()
        launch.assert_called_once()

    def test_release_guard_failure_still_attempts_exactly_one_launch(self):
        rc, events, guard, launch, _status = self._run_main_case(release_exception=True)
        self.assertEqual(rc, 0)
        self.assert_update_release_launch_order(events)
        self.assertEqual(guard.calls, 1)
        launch.assert_called_once()

    def test_successful_update_with_failed_relaunch_returns_three(self):
        rc, events, _guard, launch, _status = self._run_main_case(launch_result=(False, "cannot start"))
        self.assertEqual(rc, 3)
        self.assert_update_release_launch_order(events)
        launch.assert_called_once()

    def test_failed_update_keeps_primary_code_even_if_relaunch_fails(self):
        rc, events, _guard, launch, _status = self._run_main_case(
            update_result=(6, "failed"), launch_result=(False, "cannot start")
        )
        self.assertEqual(rc, 6)
        self.assert_update_release_launch_order(events)
        launch.assert_called_once()

    def test_launch_exception_after_success_returns_three_without_second_attempt(self):
        rc, events, _guard, launch, _status = self._run_main_case(launch_exception=RuntimeError("spawn failed"))
        self.assertEqual(rc, 3)
        self.assert_update_release_launch_order(events)
        launch.assert_called_once()

    def test_coordination_exception_before_termination_keeps_runtime_and_never_updates_or_launches(self):
        from updater import update_runner
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("updater.update_runner.nova_root", return_value=root), \
                 patch("updater.update_runner.read_version", return_value="0.9.8"), \
                 patch("updater.update_runner.coordinate_runtime_shutdown", side_effect=OSError("coordination exploded")), \
                 patch("updater.update_runner.run_update") as run_update, \
                 patch("updater.update_runner.launch_nova") as launch:
                rc = update_runner.main([])
        self.assertEqual(rc, 4)
        run_update.assert_not_called()
        launch.assert_not_called()

    def test_coordination_failure_after_verified_exit_releases_guard_and_launches_recovery_once(self):
        from updater import update_runner
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = []
            guard = _EventGuard(events)
            failed = ShutdownCoordination(
                False,
                "runtime_guard_acquire_failed:OSError",
                owner_pid=4242,
                owner_id="a" * 32,
                process_terminated=True,
                guard=guard,
            )

            def fake_launch(_root):
                events.append("launch_nova")
                return True, "ok"

            with patch("updater.update_runner.nova_root", return_value=root), \
                 patch("updater.update_runner.read_version", return_value="0.9.8"), \
                 patch("updater.update_runner.coordinate_runtime_shutdown", return_value=failed), \
                 patch("updater.update_runner.run_update") as run_update, \
                 patch("updater.update_runner._show_surviving_runtime") as show, \
                 patch("updater.update_runner.launch_nova", side_effect=fake_launch) as launch:
                rc = update_runner.main([])
        self.assertEqual(rc, 4)
        run_update.assert_not_called()
        show.assert_not_called()
        self.assertEqual(guard.calls, 1)
        launch.assert_called_once_with(root)
        self.assertEqual(events, ["release_guard", "launch_nova"])

    def test_coordination_failure_status_failure_still_attempts_show_and_never_runs_update(self):
        from updater import update_runner
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = []
            failed = ShutdownCoordination(False, "owner_process_timeout", 7, "a" * 32)
            with patch("updater.update_runner.nova_root", return_value=root), \
                 patch("updater.update_runner.read_version", return_value="0.9.8"), \
                 patch("updater.update_runner.coordinate_runtime_shutdown", return_value=failed), \
                 patch("updater.update_runner.write_status", side_effect=RuntimeError("status failed")), \
                 patch("updater.update_runner.run_update") as run_update, \
                 patch("updater.update_runner._show_surviving_runtime", side_effect=lambda *_: events.append("show") or True) as show, \
                 patch("updater.update_runner.launch_nova") as launch:
                rc = update_runner.main(["--wait-seconds", "0.01"])
        self.assertEqual(rc, 4)
        run_update.assert_not_called()
        launch.assert_not_called()
        show.assert_called_once()
        self.assertEqual(events, ["show"])


if __name__ == "__main__":
    unittest.main()
