from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from assistant.action_apps import AppTarget, classify_application, classify_document, resolve_known_application
from assistant.action_broker import ActionBroker, policy_for
from assistant.action_context import build_action_context
from assistant.action_powershell import resolve_trusted_powershell
from assistant.memory import MemoryStore
from assistant.tools import LocalTools
import assistant.tools as tools_module


class ActionApplicationSafetyTests(unittest.TestCase):
    def tools(self, root: Path) -> LocalTools:
        config = {"security": {"profile": "safe", "allowed_roots": [str(root)], "restrict_files_to_allowed_roots": True}}
        return LocalTools(config, MemoryStore(root / "memory.db"))

    def test_open_app_accepts_only_registered_aliases(self):
        self.assertTrue(classify_application("notepad").allowed)
        for value in (
            "C:/Temp/tool.exe", "tool.exe", "tool.bat", "tool.cmd", "tool.ps1",
            r"\\server\share\tool.exe", "file:///C:/Temp/tool.exe", "https://example.test/tool.exe",
            "python -c print(1)",
        ):
            with self.subTest(value=value):
                self.assertFalse(classify_application(value).allowed)
                self.assertEqual(policy_for("open_app", {"app": value}, known_tools={"open_app"}).effect, "forbidden")

    def test_documents_are_a_separate_capability_and_executables_are_forbidden(self):
        self.assertTrue(classify_document("report.pdf").allowed)
        self.assertEqual(policy_for("open_document", {"path": "report.pdf"}, known_tools={"open_document"}).effect, "reversible")
        for value in (
            "tool.exe", "tool.bat", "tool.cmd", "tool.ps1", "tool.vbs", "tool.msi",
            r"\\server\share\report.pdf", "https://example.test/report.pdf", "unknown.xyz",
        ):
            with self.subTest(value=value):
                self.assertFalse(classify_document(value).allowed)
                self.assertEqual(policy_for("open_document", {"path": value}, known_tools={"open_document"}).effect, "forbidden")

    def test_unknown_targets_never_reach_process_or_document_opener(self):
        with tempfile.TemporaryDirectory() as td:
            tools = self.tools(Path(td))
            with mock.patch.object(tools_module.subprocess, "Popen") as popen, mock.patch.object(
                tools_module.os, "startfile", create=True,
            ) as startfile:
                for value in ("tool.exe", "tool.bat", "tool.cmd", "tool.ps1", r"\\server\tool.exe"):
                    self.assertEqual(tools.open_app(value)["error"], "forbidden_action")
                self.assertEqual(tools.open_document(Path(td) / "tool.exe")["error"], "forbidden_action")
                popen.assert_not_called()
                startfile.assert_not_called()

    def test_known_alias_resolves_to_canonical_windows_location_and_uses_shell_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Windows"
            executable = root / "System32" / "notepad.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"MZ")
            resolved = resolve_known_application("notepad", windows_directory=root)
            self.assertTrue(resolved.allowed)
            self.assertEqual(resolved.path, executable.resolve())
            tools = self.tools(Path(td))
            with mock.patch.object(
                tools_module, "resolve_known_application",
                return_value=AppTarget(True, "known_application", "notepad", executable.resolve()),
            ), mock.patch.object(tools_module.subprocess, "Popen") as popen:
                result = tools.open_app("notepad")
            self.assertTrue(result["ok"])
            self.assertEqual(popen.call_args.args[0], [str(executable.resolve())])
            self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_document_change_after_approval_invalidates_permission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "report.pdf"
            target.write_bytes(b"before")
            tools = self.tools(root)
            tools.action_owner_id = "owner"; tools.action_scope = "scope"
            tools.action_session_id = "session"; tools.action_task_id = "task"
            args = {"path": str(target)}
            initial = build_action_context("open_document", args, tools=tools)
            broker = ActionBroker(
                {"security": {"profile": "safe", "approval_timeout_seconds": 2}},
                tool_names={"open_document"},
            )
            def approve_after_change(row):
                target.write_bytes(b"changed")
                self.assertTrue(broker.approve(row["request_id"]))
            broker.set_approval_handler(approve_after_change)
            result = broker.execute(
                "open_document", args, initial, lambda: self.fail("document opener must not run"),
                context_provider=lambda: build_action_context("open_document", args, tools=tools),
            )
            self.assertEqual(result["error"], "authorization_context_changed")

    def test_trusted_powershell_ignores_path_and_is_used_by_all_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Windows"
            trusted = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            trusted.parent.mkdir(parents=True)
            trusted.write_bytes(b"MZ")
            fake = Path(td) / "fake-bin" / "powershell.exe"
            fake.parent.mkdir()
            fake.write_bytes(b"FAKE")
            with mock.patch.dict(os.environ, {"PATH": str(fake.parent)}):
                self.assertEqual(resolve_trusted_powershell(windows_directory=root), trusted.resolve())

            tools = self.tools(Path(td))
            completed = mock.Mock(returncode=0, stdout="ok", stderr="")
            with mock.patch.object(tools_module, "resolve_trusted_powershell", return_value=trusted.resolve()), mock.patch.object(
                tools_module.subprocess, "run", return_value=completed,
            ) as run:
                self.assertTrue(tools.powershell("Get-Date")["ok"])
                LocalTools._ps_clipboard("Get-Clipboard -Raw")
                self.assertTrue(tools.clipboard_write("private-value")["ok"])
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                self.assertEqual(call.args[0][0], str(trusted.resolve()))
                self.assertNotEqual(call.args[0][0], str(fake.resolve()))


if __name__ == "__main__":
    unittest.main()
