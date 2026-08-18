from __future__ import annotations

import threading
import unittest

from assistant.action_context import build_action_context, current_human_intent
from assistant.agent import LocalAgent


class _Tools:
    action_session_id = "session-a"


def _agent(results, barrier):
    agent = LocalAgent.__new__(LocalAgent)
    agent.tools = _Tools()

    def core(text, *, record_conversation=True):
        barrier.wait(timeout=2)
        intent = current_human_intent()
        context = build_action_context("clipboard_read", {}, tools=agent.tools)
        results[text] = (intent.request_id if intent else None, context.explicit_intent)
        barrier.wait(timeout=2)
        return "ok"

    agent._ask_core = core
    return agent


class ActionIntentIsolationTests(unittest.TestCase):
    def test_two_concurrent_user_requests_do_not_share_intent(self):
        barrier = threading.Barrier(2)
        results = {}
        agent = _agent(results, barrier)
        threads = [
            threading.Thread(target=lambda: agent.ask("lee mi portapapeles")),
            threading.Thread(target=lambda: agent.ask("dame una respuesta")),
        ]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertEqual(results["lee mi portapapeles"][1], True)
        self.assertEqual(results["dame una respuesta"][1], False)
        self.assertNotEqual(results["lee mi portapapeles"][0], results["dame una respuesta"][0])
        self.assertIsNone(current_human_intent())

    def test_internal_request_concurrent_with_user_request_inherits_nothing(self):
        barrier = threading.Barrier(2)
        results = {}
        agent = _agent(results, barrier)
        threads = [
            threading.Thread(target=lambda: agent.ask("lee mi portapapeles")),
            threading.Thread(target=lambda: agent.ask_internal("Planner: lee mi portapapeles")),
        ]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertEqual(results["lee mi portapapeles"][1], True)
        self.assertEqual(results["Planner: lee mi portapapeles"], (None, False))

    def test_intent_is_bound_to_its_session(self):
        barrier = threading.Barrier(1)
        results = {}
        agent = _agent(results, barrier)
        intent = __import__("assistant.action_context", fromlist=["human_intent_from_text"]).human_intent_from_text(
            "lee mi portapapeles", session_id="different-session",
        )
        agent.ask_internal("step", human_intent=intent)
        self.assertEqual(results["step"], (None, False))


if __name__ == "__main__":
    unittest.main()
