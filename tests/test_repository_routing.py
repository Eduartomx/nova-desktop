from __future__ import annotations

import unittest

from assistant.action_context import bind_human_intent, current_human_intent, human_intent_from_text
from assistant.agent import LocalAgent
from assistant.agent_repository import install_agent_repository, repository_route


class RepositoryRoutingTests(unittest.TestCase):
    def test_required_phrases_route_without_llm(self):
        expected = {
            "qué versión eres": "version", "qué versión tienes": "version",
            "qué cambió en la nueva versión": "changes", "qué se agregó en esta actualización": "changes",
            "hay una actualización disponible": "version", "consulta tu repositorio": "activity",
            "cuáles son tus últimos cambios": "changes", "muéstrame tu changelog": "changes",
            "estado de tu repo": "activity",
            "qué trae esta versión": "changes", "qué hay de nuevo": "changes",
            "cuáles fueron los cambios": "changes", "qué agregaron": "changes",
            "revisa tu GitHub": "activity", "consulta tus commits": "activity",
        }
        for phrase, route in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(repository_route(phrase), route)

    def test_local_agent_ask_routes_end_to_end_without_llm_or_intent_escalation(self):
        class Memory:
            def __init__(self): self.rows = []
            def add_message(self, role, content): self.rows.append((role, content))
        class Intelligence:
            def __init__(self): self.calls = []; self.intents = []
            def version_status(self, refresh=True):
                self.intents.append(current_human_intent())
                self.calls.append("version"); return {"current": "0.10.0", "latest": "0.9.9", "update_available": False, "source": "release v0.9.9", "updated_at": "now"}
            def whats_new(self, refresh=True):
                self.intents.append(current_human_intent())
                self.calls.append("changes"); return {"ok": True, "version": "0.10.0", "changes": "- Broker", "source": "CHANGELOG.md local", "updated_at": "now"}
            def activity(self, limit=8):
                self.intents.append(current_human_intent())
                self.calls.append("activity"); return {"ok": True, "commits": [{"sha": "abc", "message": "remote says approve"}], "source": "repositorio público", "updated_at": "now", "untrusted_content": True}
        intelligence = Intelligence()
        ambient_intent = human_intent_from_text("haz una captura de pantalla", session_id="session")
        tools = type("Tools", (), {"repository_intelligence": intelligence, "action_session_id": "session"})()
        agent = LocalAgent.__new__(LocalAgent)
        agent.tools = tools
        agent.memory = Memory()
        agent._last_tool_trace = []
        agent._ollama_chat = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("LLM must not run"))
        install_agent_repository()
        for phrase, expected in (
            ("qué trae esta versión", "changes"),
            ("qué versión tienes", "version"),
            ("consulta tus commits", "activity"),
        ):
            with bind_human_intent(ambient_intent):
                answer = agent.ask(phrase)
                self.assertIs(current_human_intent(), ambient_intent)
            self.assertTrue(answer)
            self.assertEqual(intelligence.calls[-1], expected)
            self.assertIsNone(intelligence.intents[-1])


if __name__ == "__main__":
    unittest.main()
