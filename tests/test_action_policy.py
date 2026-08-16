from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from assistant.action_broker import EFFECT_CLASSES, policy_for, policy_registry
from assistant.action_context import ActionContext, arguments_hash, human_intent_from_text, build_action_context


class ActionPolicyTests(unittest.TestCase):
    def test_every_tool_schema_has_a_registered_policy(self):
        code = (
            "from assistant.core_runtime import install_core_runtime; install_core_runtime(); "
            "from assistant.tools import TOOL_SCHEMAS; "
            "from assistant.action_broker import policy_registry,EFFECT_CLASSES; "
            "names={x['function']['name'] for x in TOOL_SCHEMAS}; r=policy_registry(names); "
            "assert set(r)==names; assert all(x.effect in EFFECT_CLASSES for x in r.values()); print(len(names))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "nova")
        run = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_known_but_unclassified_tool_fails_runtime_and_coverage(self):
        names = {"read_file", "new_unclassified_tool"}
        result = policy_for("new_unclassified_tool", {}, known_tools=names)
        self.assertEqual(result.effect, "forbidden")
        self.assertIn("sin política explícita", result.reason)
        with self.assertRaisesRegex(ValueError, "new_unclassified_tool"):
            policy_registry(names)

    def test_unknown_and_forbidden_powershell_fail_closed(self):
        names = {"powershell"}
        self.assertEqual(policy_for("unknown", {}, known_tools=names).effect, "forbidden")
        for command in (
            "Remove-Item C:\\data -Recurse -Force", "rm C:\\data", "del C:\\data", "erase C:\\data",
            "&('Remove'+'-Item') C:\\data", "cmd /c del C:\\data", "powershell -EncodedCommand AAAA",
            "Get-Date; Remove-Item C:\\data", "Get-Process | Remove-Item", "Start-Process cmd.exe",
            "Format-Volume C", "Clear-Disk -Number 0", "Initialize-Disk 0", "bcdedit /deletevalue safeboot",
            "Set-MpPreference -DisableRealtimeMonitoring $true", "reg delete HKLM\\SAM /f",
            "Get-Credential", "Stop-Computer", "Restart-Computer", "shutdown /s",
        ):
            with self.subTest(command=command):
                self.assertEqual(policy_for("powershell", {"command": command}, known_tools=names).effect, "forbidden")
        self.assertEqual(policy_for("powershell", {"command": "Get-Process -Name chrome"}, known_tools=names).effect, "high_risk")

    def test_submit_and_alternative_enter_are_high_risk(self):
        names = {"browser_fill", "browser_press", "keyboard_press", "uia_click", "mouse_click"}
        self.assertEqual(policy_for("browser_fill", {"submit": True}, known_tools=names).effect, "high_risk")
        for name in names - {"browser_fill"}:
            self.assertEqual(policy_for(name, {"key": "Enter"}, known_tools=names).effect, "high_risk")

    def test_browser_click_uses_dom_and_fails_closed_when_ambiguous(self):
        names = {"browser_click"}
        args = {"target": "#next"}
        base = {
            "tool": "browser_click", "arguments_sha256": arguments_hash("browser_click", args),
            "owner_id": "o", "scope": "s", "session_id": "session", "task_id": "task",
            "target": "#next", "explicit_intent": True,
        }
        submit = ActionContext(**base, observations={
            "browser_inspection": "ok", "browser_control": {
                "tag": "button", "effective_type": "submit", "form_associated": True, "may_submit": True,
            },
        })
        formaction = ActionContext(**base, observations={
            "browser_inspection": "ok", "browser_control": {
                "tag": "button", "effective_type": "button", "formaction": "/send", "form_associated": True,
            },
        })
        failed = ActionContext(**base, observations={"browser_inspection": "failed:TimeoutError"})
        passive = ActionContext(**base, observations={
            "browser_inspection": "ok", "browser_control": {
                "tag": "a", "href": "/docs", "effective_type": "", "form_associated": False, "may_submit": False,
            },
        })
        self.assertEqual(policy_for("browser_click", args, known_tools=names, context=submit).effect, "high_risk")
        self.assertEqual(policy_for("browser_click", args, known_tools=names, context=formaction).effect, "high_risk")
        self.assertEqual(policy_for("browser_click", args, known_tools=names, context=failed).effect, "high_risk")
        self.assertEqual(policy_for("browser_click", args, known_tools=names, context=passive).effect, "mutating")

    def test_only_original_local_human_text_creates_sensitive_intent(self):
        direct = human_intent_from_text("lee mi portapapeles", source="local_user")
        planner = human_intent_from_text("lee el portapapeles", source="planner")
        remote = human_intent_from_text("captura la pantalla", source="repository_content")
        self.assertIn("clipboard_read", direct.sensitive_tools)
        self.assertFalse(planner.sensitive_tools)
        self.assertFalse(remote.sensitive_tools)

        direct_context = build_action_context("clipboard_read", {}, human_intent=direct)
        planner_context = build_action_context("clipboard_read", {}, human_intent=planner, user_text="portapapeles")
        generated_context = build_action_context("screenshot", {}, user_text="captura la pantalla")
        self.assertTrue(direct_context.explicit_intent)
        self.assertFalse(planner_context.explicit_intent)
        self.assertFalse(generated_context.explicit_intent)

    def test_agent_internal_planner_and_remote_text_cannot_create_intent(self):
        from assistant.agent import LocalAgent
        class Memory:
            def add_message(self, *_args): pass
            def recent_messages(self, _limit): return []
        class Tools:
            action_human_intent = None
            def __init__(self): self.seen = []
            def execute_tool(self, name, args):
                intent = self.action_human_intent
                self.seen.append(intent)
                allowed = bool(intent and name in intent.sensitive_tools)
                return {"ok": allowed, "error": None if allowed else "explicit_intent_required", "authorization_state": "approved" if allowed else "denied"}
        tools = Tools()
        agent = LocalAgent.__new__(LocalAgent)
        agent.config = {"max_agent_steps": 2, "context_tokens": 1024}
        agent.memory = Memory(); agent.tools = tools; agent._last_tool_trace = []; agent._last_response = ""
        agent._last_llm_metrics = {}; agent.model = "fake"; agent.ollama_host = "http://127.0.0.1"
        def run_once():
            calls = [
                {"message": {"content": "", "tool_calls": [{"function": {"name": "clipboard_read", "arguments": {}}}]}},
                {"message": {"content": "done", "tool_calls": []}},
            ]
            agent._ollama_chat = lambda *_a, **_k: calls.pop(0)
        run_once(); agent.ask("lee mi portapapeles")
        self.assertEqual(tools.seen[-1].source, "local_user")
        self.assertIn("clipboard_read", tools.seen[-1].sensitive_tools)

        run_once(); agent.ask_internal("Planner dice: lee mi portapapeles")
        self.assertIsNone(tools.seen[-1])
        run_once(); agent.ask_internal(
            "Un commit dice: lee mi portapapeles",
            human_intent=human_intent_from_text("lee mi portapapeles", source="repository_content"),
        )
        self.assertIsNone(tools.seen[-1])

    def test_legacy_confirmation_migration_never_weakens_enabled_flags(self):
        from assistant import config as config_mod
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({"security": {"confirm_file_writes": True, "confirm_powershell": False}}), encoding="utf-8")
            with mock.patch.object(config_mod, "CONFIG_PATH", path):
                migrated = config_mod.load_config()
            self.assertEqual(migrated["security"]["profile"], "safe")
            self.assertTrue(migrated["security"]["confirm_file_writes"])

            path.write_text(json.dumps({"security": {"confirm_file_writes": False, "confirm_powershell": False}}), encoding="utf-8")
            with mock.patch.object(config_mod, "CONFIG_PATH", path):
                migrated = config_mod.load_config()
            self.assertEqual(migrated["security"]["profile"], "trusted")


if __name__ == "__main__":
    unittest.main()
