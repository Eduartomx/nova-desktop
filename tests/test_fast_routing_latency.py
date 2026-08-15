from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant.agent import LocalAgent
from assistant.agent_fast_routing import fast_direct_intent
from assistant.agent_workspace import memory_query_needs_semantic
from assistant.core_runtime import architecture_status
from assistant.memory import MemoryStore


ROOT = Path(__file__).resolve().parents[1]


class FastRoutingLatencyTests(unittest.TestCase):
    def _run_runtime_probe(self) -> dict:
        script = r'''
import json
import tempfile
from pathlib import Path
from unittest import mock
from assistant.core_runtime import install_core_runtime
install_core_runtime()
from assistant.agent import LocalAgent
from assistant.memory import MemoryStore

tmp = tempfile.TemporaryDirectory()
memory = MemoryStore(Path(tmp.name) / "nova.db")
agent = LocalAgent({
    "assistant_name": "Nova",
    "model": "qwen3.5:4b",
    "workspace": {"enabled": True, "auto_memory_context": True, "semantic_context_mode": "adaptive"},
    "confidence": {"enabled": True},
    "expert_escalation": {"enabled": True, "auto_free_second_opinion": True},
}, memory=memory)

agent._ollama_chat = mock.Mock(side_effect=AssertionError("Ollama no debe ejecutarse"))
memory.search_memory = mock.Mock(side_effect=AssertionError("Semantic Memory no debe ejecutarse"))
version = agent.ask("Nova, ¿qué versión tienes?")
route = getattr(agent, "_last_fast_route", None)
ollama_calls = agent._ollama_chat.call_count
semantic_calls = memory.search_memory.call_count

agent.tools.system_status = mock.Mock(return_value={
    "ok": True,
    "cpu_percent": 12,
    "memory_percent": 34,
    "memory_used_gb": 10.5,
    "memory_total_gb": 32.0,
    "gpu": {"name": "RTX Test", "utilization_percent": 22, "memory_used_mb": 1000, "memory_total_mb": 8000},
})
system = agent.ask("estado del sistema")

memory.search_memory.reset_mock()
empty = agent.tools.memory_search("recuerdas algo", 8)

print(json.dumps({
    "version": version,
    "route": route,
    "ollama_calls": ollama_calls,
    "semantic_calls": semantic_calls,
    "system": system,
    "empty_results": empty.get("results"),
    "empty_semantic_calls": memory.search_memory.call_count,
}, ensure_ascii=False))
'''
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "nova")
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_runtime_fast_routes_bypass_llm_and_semantic_memory(self):
        row = self._run_runtime_probe()
        expected_version = (ROOT / "nova" / "NOVA_VERSION.txt").read_text(encoding="utf-8").strip()
        self.assertIn(f"Nova v{expected_version}", row["version"])
        self.assertEqual(row["route"], "version")
        self.assertEqual(row["ollama_calls"], 0)
        self.assertEqual(row["semantic_calls"], 0)
        self.assertIn("CPU 12%", row["system"])
        self.assertIn("GPU RTX Test 22%", row["system"])
        self.assertEqual(row["empty_results"], [])
        self.assertEqual(row["empty_semantic_calls"], 0)

    def test_version_classifier_does_not_steal_other_version_questions(self):
        self.assertEqual(fast_direct_intent("Nova, ¿qué versión tienes?"), "version")
        self.assertIsNone(fast_direct_intent("¿Qué versión de Python tengo?"))
        self.assertIsNone(fast_direct_intent("¿Qué versión de Ollama está instalada?"))

    def test_resident_commands_are_explicit_and_deterministic(self):
        self.assertEqual(fast_direct_intent("Nova, ¿estás ejecutándote en segundo plano?"), "resident_status")
        self.assertEqual(fast_direct_intent("Nova, ocúltate en la bandeja"), "resident_hide")
        self.assertEqual(fast_direct_intent("Nova, muéstrate"), "resident_show")
        self.assertEqual(fast_direct_intent("Nova, inicia con Windows"), "resident_autostart_on")
        self.assertEqual(fast_direct_intent("Nova, no inicies con Windows"), "resident_autostart_off")
        self.assertIsNone(fast_direct_intent("Quizás algún día podrías iniciar con Windows"))
        self.assertIsNone(fast_direct_intent("¿Conviene iniciar aplicaciones con Windows?"))

    def test_memory_semantic_context_is_adaptive(self):
        self.assertFalse(memory_query_needs_semantic("explica qué es un PID"))
        self.assertFalse(memory_query_needs_semantic("Nova, qué versión tienes"))
        self.assertTrue(memory_query_needs_semantic("¿recuerdas lo que hablamos ayer?"))
        self.assertTrue(memory_query_needs_semantic("continuemos con mi proyecto"))

    def test_local_llm_timeout_is_decoupled_from_internet_timeout(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memory = MemoryStore(Path(tmp.name) / "nova.db")
        agent = LocalAgent({
            "model": "qwen3.5:4b",
            "internet": {"timeout_seconds": 1},
            "local_llm": {"timeout_seconds": 47},
        }, memory=memory)
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
