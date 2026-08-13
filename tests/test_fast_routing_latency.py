from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant.agent_fast_routing import fast_direct_intent
from assistant.agent_workspace import memory_query_needs_semantic
from assistant.core_runtime import architecture_status, install_core_runtime
from assistant.memory import MemoryStore


class FastRoutingLatencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_core_runtime()
        from assistant.agent import LocalAgent
        cls.Agent = LocalAgent

    def make_agent(self, extra=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = MemoryStore(Path(tmp.name) / "nova.db")
        cfg = {
            "assistant_name": "Nova",
            "model": "qwen3.5:4b",
            "workspace": {"enabled": True, "auto_memory_context": True, "semantic_context_mode": "adaptive"},
            "confidence": {"enabled": True},
            "expert_escalation": {"enabled": True, "auto_free_second_opinion": True},
        }
        if extra:
            cfg.update(extra)
        return self.Agent(cfg, memory=memory), memory

    def test_version_route_is_deterministic_and_does_not_call_ollama(self):
        agent, memory = self.make_agent()
        agent._ollama_chat = mock.Mock(side_effect=AssertionError("Ollama no debe ejecutarse"))
        memory.search_memory = mock.Mock(side_effect=AssertionError("Semantic Memory no debe ejecutarse"))
        result = agent.ask("Nova, ¿qué versión tienes?")
        self.assertIn("Nova v0.9.2", result)
        self.assertEqual(getattr(agent, "_last_fast_route", None), "version")
        agent._ollama_chat.assert_not_called()
        memory.search_memory.assert_not_called()

    def test_system_status_route_uses_structured_tool_without_llm(self):
        agent, _memory = self.make_agent()
        agent._ollama_chat = mock.Mock(side_effect=AssertionError("Ollama no debe ejecutarse"))
        agent.tools.system_status = mock.Mock(return_value={
            "ok": True,
            "cpu_percent": 12,
            "memory_percent": 34,
            "memory_used_gb": 10.5,
            "memory_total_gb": 32.0,
            "gpu": {"name": "RTX Test", "utilization_percent": 22, "memory_used_mb": 1000, "memory_total_mb": 8000},
        })
        result = agent.ask("estado del sistema")
        self.assertIn("CPU 12%", result)
        self.assertIn("GPU RTX Test 22%", result)
        agent._ollama_chat.assert_not_called()

    def test_version_classifier_does_not_steal_other_version_questions(self):
        self.assertEqual(fast_direct_intent("Nova, ¿qué versión tienes?"), "version")
        self.assertIsNone(fast_direct_intent("¿Qué versión de Python tengo?"))
        self.assertIsNone(fast_direct_intent("¿Qué versión de Ollama está instalada?"))

    def test_memory_semantic_context_is_adaptive(self):
        self.assertFalse(memory_query_needs_semantic("explica qué es un PID"))
        self.assertFalse(memory_query_needs_semantic("Nova, qué versión tienes"))
        self.assertTrue(memory_query_needs_semantic("¿recuerdas lo que hablamos ayer?"))
        self.assertTrue(memory_query_needs_semantic("continuemos con mi proyecto"))

    def test_empty_memory_search_tool_never_calls_semantic_engine(self):
        agent, memory = self.make_agent()
        memory.search_memory = mock.Mock(side_effect=AssertionError("No debe buscar embeddings con 0 recuerdos"))
        result = agent.tools.memory_search("recuerdas algo", 8)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [])
        memory.search_memory.assert_not_called()

    def test_local_llm_timeout_is_decoupled_from_internet_timeout(self):
        agent, _memory = self.make_agent({
            "internet": {"timeout_seconds": 1},
            "local_llm": {"timeout_seconds": 47},
        })
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"message": {"content": "OK"}}
        with mock.patch("assistant.agent.requests.post", return_value=response) as post:
            agent._ollama_chat([{"role": "user", "content": "hola"}])
        self.assertEqual(post.call_args.kwargs["timeout"], 47.0)

    def test_architecture_exposes_fast_routing(self):
        status = architecture_status()
        self.assertIn("fast_routing", status["github_managed_native"])
        self.assertIn("adaptive_memory_context", status["github_managed_native"])


if __name__ == "__main__":
    unittest.main()
