from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from assistant.action_broker import ActionBroker
from assistant.action_context import ActionContext, arguments_hash
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


class _BrokerTools:
    def __init__(self, broker):
        self.action_broker = broker
        self.action_task_id = ""
        self.action_owner_id = "owner"
        self.action_scope = "scope"
        self.action_session_id = "session"


class _BrokerAgent:
    def __init__(self, memory, broker):
        self.memory = memory
        self.config = {"task_engine": {"enabled": True, "max_step_retries": 2, "auto_replan": True}}
        self.tools = _BrokerTools(broker)
        self.calls = 0
        self.effects = 0
        self.trace = []
        self.change_context = False

    def ask_internal(self, _instruction, *, human_intent=None):
        self.calls += 1
        if self.calls > 1:
            self.trace = []
            return "Segundo paso completado."
        args = {"path": "fixture.txt", "content": "secret"}

        def make_context():
            return ActionContext(
                tool="write_file", arguments_sha256=arguments_hash("write_file", args),
                owner_id=self.tools.action_owner_id, scope=self.tools.action_scope,
                session_id=("changed" if self.change_context else self.tools.action_session_id),
                task_id=self.tools.action_task_id, target="fixture.txt", explicit_intent=True,
                observations={"stable": not self.change_context},
            )

        initial = make_context()
        self.change_context = False
        result = self.tools.action_broker.execute(
            "write_file", args, initial,
            lambda: setattr(self, "effects", self.effects + 1) or {"ok": True},
            context_provider=make_context,
        )
        self.trace = [{
            "name": "write_file", "ok": bool(result.get("ok")),
            "authorization_state": result.get("authorization_state"), "error": result.get("error"),
        }]
        return "Paso completado." if result.get("ok") else str(result.get("error") or result.get("authorization_state"))

    def ask(self, text):
        return self.ask_internal(text)

    def last_tool_trace(self):
        return list(self.trace)

    def _ollama_chat(self, *_args, **_kwargs):
        raise AssertionError("must not replan")


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

    def test_real_broker_waits_and_continues_same_task_and_step_once(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            broker = ActionBroker(
                {"security": {"profile": "balanced", "approval_timeout_seconds": 5}},
                tool_names={"write_file"},
            )
            agent = _BrokerAgent(memory, broker)
            engine = TaskEngine(agent, memory=memory)
            shown = threading.Event()
            rows = []
            broker.set_approval_handler(lambda row: (rows.append(row), shown.set()))
            output = []
            plan = {"steps": [
                {"description": "escribe", "success_criteria": "archivo"},
                {"description": "verifica", "success_criteria": "hecho"},
            ]}
            worker = threading.Thread(target=lambda: output.append(engine.run("objetivo", plan=plan)), daemon=True)
            worker.start()
            self.assertTrue(shown.wait(1))
            task_id = engine.current_task_id
            self.assertIsNotNone(task_id)
            self.assertEqual(memory.get_task(task_id)["status"], "waiting_for_approval")
            resumed = engine.resume()
            self.assertEqual(resumed["task_id"], task_id)
            self.assertEqual(resumed["status"], "waiting_for_approval")
            self.assertTrue(broker.approve(rows[0]["request_id"]))
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(output[0]["task_id"], task_id)
            self.assertEqual(output[0]["status"], "completed")
            self.assertEqual(agent.effects, 1)
            self.assertEqual(agent.calls, 2)
            self.assertEqual([row["index"] for row in output[0]["steps"]], [1, 2])

    def test_headless_approval_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            broker = ActionBroker({"security": {"profile": "balanced"}}, tool_names={"write_file"})
            result = TaskEngine(_BrokerAgent(memory, broker), memory=memory).run(
                "objetivo", plan={"steps": [{"description": "escribe", "success_criteria": "archivo"}]},
            )
            self.assertEqual(result["status"], "approval_ui_unavailable")

    def test_real_broker_denial_expiry_shutdown_and_context_change_are_terminal(self):
        cases = ("denied", "expired", "cancelled", "context_changed")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                now = [1.0]
                broker = ActionBroker(
                    {"security": {"profile": "balanced", "approval_timeout_seconds": 5}},
                    tool_names={"write_file"}, clock=lambda: now[0],
                )
                memory = MemoryStore(Path(td) / "memory.db")
                agent = _BrokerAgent(memory, broker)
                engine = TaskEngine(agent, memory=memory)
                shown = threading.Event()
                rows = []
                broker.set_approval_handler(lambda row: (rows.append(row), shown.set()))
                output = []
                worker = threading.Thread(target=lambda: output.append(engine.run(
                    "objetivo", plan={"steps": [{"description": "escribe", "success_criteria": "archivo"}]},
                )), daemon=True)
                worker.start()
                self.assertTrue(shown.wait(1))
                if case == "denied":
                    broker.deny(rows[0]["request_id"])
                elif case == "expired":
                    now[0] = 10.0
                    broker.approve(rows[0]["request_id"])
                elif case == "cancelled":
                    broker.cancel_all("shutdown", shutdown=True)
                else:
                    agent.change_context = True
                    broker.approve(rows[0]["request_id"])
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(agent.effects, 0)
                self.assertNotEqual(output[0]["status"], "completed")

    def test_process_reopen_expires_unrecoverable_approval(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            task_id = memory.create_task("objetivo", {"steps": [{"index": 1, "description": "escribe", "success_criteria": "archivo"}]}, status="waiting_for_approval")
            memory.upsert_task_step(task_id, 1, "escribe", "archivo", status="waiting_for_approval", attempts=1)
            agent = _Agent(memory)
            TaskEngine(agent, memory=memory)
            task = memory.get_task(task_id)
            self.assertEqual(task["status"], "expired")
            self.assertEqual(task["steps"][0]["status"], "expired")

    def test_process_reopen_expires_every_waiting_task_beyond_first_page(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "memory.db")
            task_ids = []
            for index in range(135):
                task_id = memory.create_task(
                    f"objetivo-{index}",
                    {"steps": [{"index": 1, "description": "escribe", "success_criteria": "archivo"}]},
                    status="waiting_for_approval",
                )
                memory.upsert_task_step(task_id, 1, "escribe", "archivo", status="waiting_for_approval", attempts=1)
                task_ids.append(task_id)
            TaskEngine(_Agent(memory), memory=memory)
            self.assertEqual(
                [memory.get_task(task_id)["status"] for task_id in task_ids],
                ["expired"] * len(task_ids),
            )
            self.assertTrue(all(memory.get_task(task_id)["steps"][0]["status"] == "expired" for task_id in task_ids))


if __name__ == "__main__":
    unittest.main()
