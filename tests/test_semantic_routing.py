import unittest

from nova.assistant.v063_agent import (
    _format_reindex_result,
    _format_status_result,
    semantic_direct_intent,
)


class SemanticRoutingTests(unittest.TestCase):
    def test_reindex_spanish_routes_directly(self):
        action, params = semantic_direct_intent("Nova, reindexa la memoria semántica.")
        self.assertEqual(action, "reindex")
        self.assertTrue(params["force"])
        self.assertFalse(params["workspace_only"])

    def test_reindex_without_accent_routes_directly(self):
        action, _ = semantic_direct_intent("reindexar memoria semantica")
        self.assertEqual(action, "reindex")

    def test_project_scope_is_detected(self):
        action, params = semantic_direct_intent("Regenera la memoria semántica solo de este proyecto")
        self.assertEqual(action, "reindex")
        self.assertTrue(params["workspace_only"])

    def test_status_routes_directly(self):
        action, params = semantic_direct_intent("¿Está funcionando tu memoria semántica?")
        self.assertEqual(action, "status")
        self.assertTrue(params["refresh"])

    def test_unrelated_windows_index_is_not_intercepted(self):
        action, _ = semantic_direct_intent("reinicia el indexador de Windows Search")
        self.assertIsNone(action)

    def test_reindex_failure_reports_ollama_command(self):
        text = _format_reindex_result({
            "ok": False,
            "detail": "Falta qwen3-embedding:0.6b",
            "install_command": "ollama pull qwen3-embedding:0.6b",
        })
        self.assertIn("ollama pull qwen3-embedding:0.6b", text)
        self.assertNotIn("Windows Search", text)

    def test_status_success_is_human_readable(self):
        text = _format_status_result({
            "ok": True,
            "status": {
                "enabled": True,
                "model_available": True,
                "model": "qwen3-embedding:0.6b",
                "indexed": 8,
                "pending": 2,
                "total_candidates": 10,
                "detail": "modelo disponible",
            },
        })
        self.assertIn("8/10", text)
        self.assertIn("qwen3-embedding:0.6b", text)


if __name__ == "__main__":
    unittest.main()
