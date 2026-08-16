from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.memory import MemoryStore
from assistant.task_engine import TaskEngine


class _Agent:
    def __init__(self, memory, authorization_state="pending"):
        self.memory = memory
        self.config = {"task_engine": {"enabled": True, "max_step_retries": 2, "auto_replan": True}}
        self.calls = 0
        self.replans = 0
        self.authorization_state = authorization_state
    def ask(self, _text):
        self.calls += 1
        return "Esperando aprobación local para continuar la acción." if self.authorization_state == "pending" else "La acción fue denegada y no produjo efectos."
    def last_tool_trace(self):
        return [{"name": "write_file", "ok": False, "authorization_state": self.authorization_state}]
    def _ollama_chat(self, *_args, **_kwargs):
        self.replans += 1
        return {"message": {"content": '{"steps":[]}'}}


class ActionTaskEngineTests(unittest.TestCase):
    def test_waiting_is_not_failure_retry_or_replan(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            agent = _Agent(memory)
            engine = TaskEngine(agent, memory=memory)
            result = engine.run("escribir", plan={"steps": [{"description": "escribe", "success_criteria": "archivo"}]})
            self.assertEqual(result["status"], "waiting_for_approval")
            self.assertEqual(agent.calls, 1)
            self.assertEqual(agent.replans, 0)
            self.assertEqual(memory.get_task(result["task_id"])["status"], "waiting_for_approval")

    def test_cancel_clears_pending_broker_when_present(self):
        class Broker:
            def __init__(self): self.calls = []
            def cancel_all(self, reason): self.calls.append(reason)
        agent = _Agent(None)
        agent.tools = type("Tools", (), {"action_broker": Broker()})()
        engine = TaskEngine(agent)
        engine.cancel()
        self.assertEqual(agent.tools.action_broker.calls, ["task_cancelled"])

    def test_denied_does_not_retry_or_replan(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            agent = _Agent(memory, authorization_state="denied")
            result = TaskEngine(agent, memory=memory).run(
                "escribir", plan={"steps": [{"description": "escribe", "success_criteria": "archivo"}]},
            )
            self.assertEqual(result["status"], "denied")
            self.assertEqual(agent.calls, 1)
            self.assertEqual(agent.replans, 0)


if __name__ == "__main__":
    unittest.main()
