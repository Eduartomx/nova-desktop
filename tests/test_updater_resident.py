from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from assistant.runtime_lifecycle import RuntimeLifecycleManager
from assistant.ui_workspace import _start_update_supervisor
from updater import nova_updater


class _StatusVar:
    def __init__(self):
        self.values = []

    def set(self, value):
        self.values.append(str(value))


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

    def test_ui_update_uses_runner_parent_pid_without_local_immediate_shutdown(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "nova" / "assistant" / "ui_workspace.py").read_text(encoding="utf-8")
        self.assertIn("update_runner.py", source)
        self.assertIn("--parent-pid", source)
        self.assertNotIn("self.request_shutdown('update')", source)
        self.assertNotIn('self.request_shutdown("update")', source)

    def test_ui_successful_popen_does_not_request_local_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            updater_dir = root / "updater"
            updater_dir.mkdir()
            (updater_dir / "update_runner.py").write_text("# test\n", encoding="utf-8")
            ui = SimpleNamespace(status_var=_StatusVar(), request_shutdown=mock.Mock())
            with mock.patch("assistant.ui_workspace.subprocess.Popen") as popen:
                ok = _start_update_supervisor(ui, root=root, show_error=lambda *_: self.fail("unexpected UI error"))
        self.assertTrue(ok)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("--parent-pid", command)
        ui.request_shutdown.assert_not_called()
        self.assertIn("permanecerá activa", ui.status_var.values[-1])

    def test_ui_failed_popen_keeps_runtime_active_and_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            updater_dir = root / "updater"
            updater_dir.mkdir()
            (updater_dir / "update_runner.py").write_text("# test\n", encoding="utf-8")
            ui = SimpleNamespace(status_var=_StatusVar(), request_shutdown=mock.Mock())
            errors = []
            with mock.patch("assistant.ui_workspace.subprocess.Popen", side_effect=OSError("spawn failed")) as popen:
                ok = _start_update_supervisor(ui, root=root, show_error=lambda title, message: errors.append((title, message)))
        self.assertFalse(ok)
        popen.assert_called_once()
        ui.request_shutdown.assert_not_called()
        self.assertTrue(errors)
        self.assertIn("spawn failed", errors[0][1])
        self.assertIn("continúa abierta", ui.status_var.values[-1])

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
