from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assistant.agent import LocalAgent
from assistant.memory import MemoryStore
from assistant.task_engine import TaskEngine
from assistant.tools import LocalTools
from assistant.ui import AssistantUI


class _FakeTools:
    def __init__(self):
        self.calls = []

    def execute_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return {"ok": True, "value": 42}


class _FakeTaskAgent:
    def __init__(self, memory):
        self.memory = memory
        self.config = {"task_engine": {"enabled": True, "max_step_retries": 0, "stop_on_failed_step": True}}
        self.calls = []

    def ask(self, text):
        self.calls.append(text)
        return "Paso completado y verificado con evidencia local."


class NativeCoreTests(unittest.TestCase):
    def test_local_tools_security_contract_and_dangerous_powershell_guard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            memory = MemoryStore(root / "memory.db")
            tools = LocalTools(
                {"security": {"profile": "trusted", "restrict_files_to_allowed_roots": True, "allowed_roots": [str(root)]}},
                memory,
            )
            target = root / "hello.txt"
            result = tools.write_file(str(target), "hola")
            self.assertTrue(result["ok"])
            read = tools.read_file(str(target))
            self.assertEqual(read["content"], "hola")
            blocked = tools.powershell("Remove-Item -Recurse C:\\Temp")
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"], "forbidden_action")

    def test_agent_tool_loop_works_without_historical_local_agent(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            fake_tools = _FakeTools()
            agent = LocalAgent({"model": "fake", "recent_messages": 4}, memory=memory, tools=fake_tools)
            replies = iter([
                {"message": {"content": "", "tool_calls": [{"function": {"name": "probe", "arguments": {"x": 1}}}]}},
                {"message": {"content": "Resultado comprobado."}},
            ])
            agent._ollama_chat = lambda messages, tools=None, timeout=None: next(replies)
            result = agent.ask("comprueba esto")
            self.assertEqual(result, "Resultado comprobado.")
            self.assertEqual(fake_tools.calls, [("probe", {"x": 1})])
            self.assertEqual(agent.last_tool_trace()[0]["name"], "probe")
            self.assertEqual(memory.recent_messages(2)[-1]["role"], "assistant")

    def test_task_engine_persists_execution_using_native_memory(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            agent = _FakeTaskAgent(memory)
            engine = TaskEngine(agent, memory=memory)
            plan = {
                "goal": "Prueba",
                "steps": [
                    {"index": 1, "description": "Comprobar A", "success_criteria": "A correcto"},
                    {"index": 2, "description": "Comprobar B", "success_criteria": "B correcto"},
                ],
            }
            result = engine.run("Prueba", plan=plan)
            self.assertTrue(result["ok"])
            self.assertEqual(len(agent.calls), 2)
            stored = memory.get_task(result["task_id"])
            self.assertEqual(stored["status"], "completed")
            self.assertEqual([x["status"] for x in stored["steps"]], ["completed", "completed"])

    def test_ui_exposes_stable_adapter_contract(self):
        for name in ("_build", "_append", "_send", "_close", "_show_window", "_start_recording", "_stop_recording"):
            self.assertTrue(callable(getattr(AssistantUI, name, None)), name)

    def test_full_core_runtime_installs_in_clean_subprocess(self):
        code = (
            "from assistant.core_runtime import install_core_runtime, architecture_status; "
            "install_core_runtime(); s=architecture_status(); "
            "assert s['ok']; assert not s['unmanaged_core_files']; "
            "from assistant.tools import TOOL_SCHEMAS; "
            "names={x['function']['name'] for x in TOOL_SCHEMAS}; "
            "assert {'browser_open','uia_snapshot','skill_run','confidence_status'} <= names; "
            "print('native-core-ok')"
        )
        env = dict(os.environ)
        nova_dir = Path(__file__).resolve().parents[1] / "nova"
        env["PYTHONPATH"] = str(nova_dir) + os.pathsep + env.get("PYTHONPATH", "")
        cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(cp.returncode, 0, cp.stdout + "\n" + cp.stderr)
        self.assertIn("native-core-ok", cp.stdout)


if __name__ == "__main__":
    unittest.main()
