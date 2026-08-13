from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.agent_learning import learning_direct_intent
from assistant.core_runtime import architecture_status
from assistant.learn_from_expert import ExpertLearning


class FakeMemory:
    def __init__(self):
        self.rows = []

    def active_workspace(self):
        return {"id": 7, "name": "Nova"}

    def set_memory(self, key, value, category="fact", workspace_id=None):
        self.rows.append((key, value, category, workspace_id))


class FakeRegistry:
    def __init__(self):
        self.saved = []

    def save(self, **kwargs):
        self.saved.append(kwargs)
        return {"id": 42, "name": kwargs["name"], "trust_level": kwargs.get("trust_level")}


class FakeExpert:
    def __init__(self, result=None):
        self._result = result or {}
        self._last_candidate = {
            "problem": "Servidor falla al iniciar",
            "local_answer": "Hipótesis local incompleta",
        }
        self._imported_chatgpt = {}
        self._pending_chatgpt = {}

    def last_result(self):
        return dict(self._result)


class LearnFromExpertTests(unittest.TestCase):
    def make_service(self, td, memory=None, **cfg):
        config = {
            "enabled": True,
            "auto_capture": True,
            "auto_learn": False,
            "require_verification": True,
            "allow_user_confirmation": True,
            **cfg,
        }
        return ExpertLearning(config, memory=memory, db_path=Path(td) / "learning.db")

    def test_external_response_is_memory_only(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            secret_answer = "EXTERNAL_PRIVATE_ANSWER revisa el log y reinicia el servicio"
            result = service.capture(
                provider="groq",
                model="openai/gpt-oss-120b",
                response=secret_answer,
                verdict="agree",
                packet_sha256="a" * 64,
                problem="PRIVATE_PROBLEM",
                local_answer="PRIVATE_LOCAL_ANSWER",
            )
            self.assertTrue(result["ok"])
            candidate = service.candidate(include_content=True)
            self.assertIn("EXTERNAL_PRIVATE_ANSWER", candidate["response"])
            raw = Path(td, "learning.db").read_bytes()
            self.assertNotIn(b"EXTERNAL_PRIVATE_ANSWER", raw)
            self.assertNotIn(b"PRIVATE_PROBLEM", raw)
            self.assertNotIn(b"PRIVATE_LOCAL_ANSWER", raw)

    def test_unverified_candidate_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            service.capture(provider="groq", model="gpt", response="solución suficientemente larga", verdict="agree")
            result = service.save_skill(
                name="Reparar servicio",
                description="Procedimiento",
                triggers=["reparar servicio"],
                steps=[{"instruction": "Revisar estado del servicio"}],
                verification=["Confirmar que inicia"],
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "expert_solution_not_verified")

    def test_verified_solution_becomes_draft_skill_and_optional_memory(self):
        with tempfile.TemporaryDirectory() as td:
            memory = FakeMemory()
            service = self.make_service(td, memory=memory)
            registry = FakeRegistry()
            service.capture(
                provider="groq",
                model="openai/gpt-oss-120b",
                response="comprobar configuración y volver a iniciar",
                verdict="partially_agree",
                packet_sha256="b" * 64,
            )
            verified = service.verify(True, source="tool", note="system_status confirmó recuperación")
            self.assertTrue(verified["verified"])
            with patch("assistant.learn_from_expert.get_skill_registry", return_value=registry):
                result = service.save_skill(
                    name="Recuperar servicio",
                    description="Procedimiento verificado",
                    triggers=["recuperar servicio"],
                    steps=[
                        {"title": "Comprobar", "instruction": "Revisar el estado actual", "verify": "Debe responder"},
                        {"title": "Reintentar", "instruction": "Reintentar la operación normal"},
                    ],
                    verification=["Confirmar estado saludable"],
                    workspace=True,
                    memory_summary="La recuperación requiere comprobar el estado y verificar después.",
                )
            self.assertTrue(result["ok"])
            saved = registry.saved[0]
            self.assertEqual(saved["trust_level"], "draft")
            self.assertEqual(saved["source"], "expert_verified")
            self.assertEqual(saved["workspace_id"], 7)
            self.assertEqual(saved["provenance"]["provider"], "groq")
            self.assertNotIn("comprobar configuración", str(saved["provenance"]))
            self.assertTrue(memory.rows)
            self.assertEqual(memory.rows[0][2], "learned_procedure")
            self.assertEqual(memory.rows[0][3], 7)
            self.assertEqual(service.candidate(), {})

    def test_failed_verification_blocks_learning(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            service.capture(provider="chatgpt_web", model="subscription", response="respuesta experta con pasos")
            service.verify(False, source="user", note="no funcionó")
            self.assertFalse(service.candidate()["verified"])

    def test_capture_latest_free_opinion(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            expert = FakeExpert({
                "ok": True,
                "provider": "groq",
                "model": "openai/gpt-oss-120b",
                "verdict": "agree",
                "response": "SECOND_OPINION_CONTENT con comprobación adicional",
                "packet_sha256": "c" * 64,
            })
            result = service.capture_latest_from_expert(expert)
            self.assertTrue(result["ok"])
            self.assertEqual(service.candidate()["provider"], "groq")

    def test_routing_defaults_and_architecture(self):
        self.assertEqual(learning_direct_intent("Nova, esto funcionó"), "verify_success")
        self.assertEqual(learning_direct_intent("Aprende esta solución"), "learn")
        self.assertEqual(learning_direct_intent("No aprendas esto"), "discard")
        self.assertEqual(learning_direct_intent("Estado de aprendizaje experto"), "status")
        status = architecture_status()
        self.assertIn("learn_from_expert", status["github_managed_native"])

    def test_auto_learn_is_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            status = service.status()
            self.assertFalse(status["auto_learn"])
            self.assertFalse(status["persists_external_content"])


if __name__ == "__main__":
    unittest.main()
