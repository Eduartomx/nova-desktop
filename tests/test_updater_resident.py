from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from assistant.runtime_lifecycle import RuntimeLifecycleManager
from assistant.ui_workspace import _mark_update_status_displayed, _poll_update_supervisor, _start_update_supervisor
from updater import nova_updater


class _StatusVar:
    def __init__(self):
        self.values = []

    def set(self, value):
        self.values.append(str(value))


class _Button:
    def __init__(self):
        self.states = []
        self.state = "normal"

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
            self.states.append(self.state)


class _Root:
    def __init__(self):
        self.scheduled = []

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))
        return len(self.scheduled)


class _Proc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.wait = mock.Mock(side_effect=AssertionError("Tk updater tracking must never call wait()"))
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        return self.returncode


class ResidentUpdaterTests(unittest.TestCase):
    def test_direct_update_delegates_to_supervisor_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            updater_dir = root / "updater"
            updater_dir.mkdir()
            runner = updater_dir / "update_runner.py"
            runner.write_text("# test\n", encoding="utf-8")
            with mock.patch.object(nova_updater, "ROOT", root):
                with mock.patch("updater.nova_updater.subprocess.call", return_value=0) as call:
                    result = nova_updater._delegate_direct_update()
        self.assertEqual(result, 0)
        call.assert_called_once()
        self.assertTrue(str(call.call_args.args[0][-1]).endswith("update_runner.py"))

    def test_direct_update_without_supervisor_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(nova_updater, "ROOT", Path(td)):
                result = nova_updater._delegate_direct_update()
        self.assertEqual(result, 4)

    def test_normal_direct_main_never_falls_through_to_file_sync(self):
        with mock.patch.object(nova_updater, "_delegate_direct_update", return_value=4) as delegated, \
             mock.patch.object(nova_updater, "sync_release") as sync, \
             mock.patch("sys.argv", ["nova_updater.py"]):
            result = nova_updater.main()
        self.assertEqual(result, 4)
        delegated.assert_called_once()
        sync.assert_not_called()

    def test_check_path_remains_non_supervised(self):
        with mock.patch.object(nova_updater, "check_only", return_value=10) as check:
            with mock.patch.object(nova_updater, "_delegate_direct_update") as delegated:
                with mock.patch("sys.argv", ["nova_updater.py", "--check"]):
                    result = nova_updater.main()
        self.assertEqual(result, 10)
        check.assert_called_once()
        delegated.assert_not_called()

    def _ui_fixture(self):
        return SimpleNamespace(
            status_var=_StatusVar(),
            request_shutdown=mock.Mock(),
            update_button=_Button(),
            root=_Root(),
            _update_supervisor_active=False,
            _update_supervisor_process=None,
            _append=mock.Mock(),
        )

    def _root_fixture(self, td):
        root = Path(td)
        updater_dir = root / "updater"
        updater_dir.mkdir()
        (updater_dir / "update_runner.py").write_text("# test\n", encoding="utf-8")
        return root

    def test_ui_update_uses_runner_parent_pid_without_local_immediate_shutdown(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "nova" / "assistant" / "ui_workspace.py").read_text(encoding="utf-8")
        self.assertIn("update_runner.py", source)
        self.assertIn("--parent-pid", source)
        self.assertNotIn("self.request_shutdown('update')", source)
        self.assertNotIn('self.request_shutdown("update")', source)
        self.assertNotIn("path.unlink(missing_ok=True)", source)

    def test_ui_successful_popen_tracks_process_disables_button_and_does_not_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_fixture(td)
            ui = self._ui_fixture()
            proc = _Proc(None)
            with mock.patch("assistant.ui_workspace.subprocess.Popen", return_value=proc) as popen:
                ok = _start_update_supervisor(ui, root=root, show_error=lambda *_: self.fail("unexpected UI error"))
        self.assertTrue(ok)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("--parent-pid", command)
        self.assertIs(ui._update_supervisor_process, proc)
        self.assertTrue(ui._update_supervisor_active)
        self.assertEqual(ui.update_button.state, "disabled")
        self.assertTrue(ui.root.scheduled)
        ui.request_shutdown.assert_not_called()
        proc.wait.assert_not_called()

    def test_rapid_double_click_starts_only_one_supervisor(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_fixture(td)
            ui = self._ui_fixture()
            proc = _Proc(None)
            with mock.patch("assistant.ui_workspace.subprocess.Popen", return_value=proc) as popen:
                first = _start_update_supervisor(ui, root=root)
                second = _start_update_supervisor(ui, root=root)
        self.assertTrue(first)
        self.assertFalse(second)
        popen.assert_called_once()
        self.assertIn("ya en curso", ui.status_var.values[-1].lower())
        ui.request_shutdown.assert_not_called()

    def test_ui_failed_popen_keeps_runtime_active_restores_state_and_button(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_fixture(td)
            ui = self._ui_fixture()
            ui.update_button.state = "disabled"
            errors = []
            with mock.patch("assistant.ui_workspace.subprocess.Popen", side_effect=OSError("spawn failed")) as popen:
                ok = _start_update_supervisor(ui, root=root, show_error=lambda title, message: errors.append((title, message)))
        self.assertFalse(ok)
        popen.assert_called_once()
        self.assertFalse(ui._update_supervisor_active)
        self.assertIsNone(ui._update_supervisor_process)
        self.assertEqual(ui.update_button.state, "normal")
        ui.request_shutdown.assert_not_called()
        self.assertIn("spawn failed", errors[0][1])
        self.assertIn("continúa abierta", ui.status_var.values[-1])

    def test_poll_while_supervisor_active_is_nonblocking_and_reschedules(self):
        ui = self._ui_fixture()
        proc = _Proc(None)
        ui._update_supervisor_active = True
        ui._update_supervisor_process = proc
        _poll_update_supervisor(ui, root=Path(tempfile.gettempdir()), consume_status=mock.Mock())
        self.assertTrue(ui._update_supervisor_active)
        self.assertTrue(ui.root.scheduled)
        proc.wait.assert_not_called()
        self.assertIn("en curso", ui.status_var.values[-1].lower())

    def test_supervisor_return_four_consumes_result_and_reenables_button(self):
        ui = self._ui_fixture()
        proc = _Proc(4)
        ui._update_supervisor_active = True
        ui._update_supervisor_process = proc
        ui.update_button.state = "disabled"
        consumed = mock.Mock(return_value=True)
        _poll_update_supervisor(ui, root=Path(tempfile.gettempdir()), consume_status=consumed)
        consumed.assert_called_once_with(only_if_new=True)
        self.assertFalse(ui._update_supervisor_active)
        self.assertEqual(ui.update_button.state, "normal")
        proc.wait.assert_not_called()

    def test_supervisor_return_five_is_reported_without_corruption_message(self):
        ui = self._ui_fixture()
        proc = _Proc(5)
        ui._update_supervisor_active = True
        ui._update_supervisor_process = proc
        ui.update_button.state = "disabled"
        consumed = mock.Mock()
        _poll_update_supervisor(ui, root=Path(tempfile.gettempdir()), consume_status=consumed)
        consumed.assert_not_called()
        self.assertEqual(ui.update_button.state, "normal")
        self.assertIn("ya existe una actualización en curso", ui.status_var.values[-1].lower())
        proc.wait.assert_not_called()

    def test_result_callback_runs_in_same_session_after_supervisor_exits(self):
        ui = self._ui_fixture()
        proc = _Proc(4)
        ui._update_supervisor_active = True
        ui._update_supervisor_process = proc
        calls = []
        _poll_update_supervisor(
            ui,
            root=Path(tempfile.gettempdir()),
            consume_status=lambda **kwargs: calls.append(kwargs) or True,
        )
        self.assertEqual(calls, [{"only_if_new": True}])
        self.assertFalse(ui._update_supervisor_active)
        self.assertEqual(ui.update_button.state, "normal")

    def test_displayed_update_state_is_preserved_for_doctor_instead_of_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "update_last.json"
            original = {
                "ok": False,
                "timestamp": "2026-08-14T00:00:00+00:00",
                "state": "coordination_failed",
                "before": "0.9.8",
                "after": "0.9.8",
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            _mark_update_status_displayed(path, original)
            self.assertTrue(path.is_file())
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(saved["displayed"])
        self.assertEqual(saved["state"], "coordination_failed")
        self.assertEqual(saved["before"], "0.9.8")
        self.assertEqual(saved["after"], "0.9.8")

    def test_shutdown_for_update_mailbox_command_maps_to_update_lifecycle_reason(self):
        class Root:
            def after(self, *_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as td:
            lifecycle = RuntimeLifecycleManager(Root(), data_root=Path(td))
            with mock.patch.object(lifecycle, "request_shutdown", return_value=True) as request:
                result = lifecycle.handle_control_command("shutdown_for_update")
        self.assertTrue(result["ok"])
        request.assert_called_once_with("update")

    def test_cmd_uses_same_runner_and_has_no_direct_updater_fallback(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "nova" / "ACTUALIZAR_NOVA.cmd").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("updater\\update_runner.py", source)
        self.assertNotIn("updater\\nova_updater.py", source)
        self.assertIn("exit /b 4", source)


if __name__ == "__main__":
    unittest.main()
