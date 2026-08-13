from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant import tools as tools_mod
from assistant.agent import LocalAgent
from assistant.memory import MemoryStore


class _FakeTools:
    def execute_tool(self, name, arguments=None):
        return {"ok": True}


class NativeAgentSelectorTests(unittest.TestCase):
    def test_agent_resolves_tool_selector_dynamically(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            agent = LocalAgent({"model": "fake", "recent_messages": 0}, memory=memory, tools=_FakeTools())
            original = tools_mod.select_tool_schemas
            sentinel = [{"type": "function", "function": {"name": "dynamic_domain_tool", "parameters": {"type": "object", "properties": []}}}]
            captured = []
            try:
                tools_mod.select_tool_schemas = lambda _text: sentinel
                def fake_chat(messages, tools=None, timeout=None):
                    captured.append(tools)
                    return {"message": {"content": "ok"}}
                agent._ollama_chat = fake_chat
                self.assertEqual(agent.ask("prueba selector"), "ok")
            finally:
                tools_mod.select_tool_schemas = original
            self.assertEqual(captured[0], sentinel)


if __name__ == "__main__":
    unittest.main()
