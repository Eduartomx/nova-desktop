from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from assistant.expert_resilience import (
    ProviderHTTPError,
    install_expert_resilience,
    normalize_expert_config,
)

install_expert_resilience()

from assistant.core_runtime import architecture_status
from assistant.expert_escalation import ExpertEscalation


class ExpertResilienceTests(unittest.TestCase):
    @staticmethod
    def assessment():
        return {
            "request_kind": "diagnosis",
            "risk_level": "normal",
            "score": 0.40,
            "band": "low",
            "evidence_count": 1,
            "verification_count": 0,
            "failure_count": 1,
            "contradiction_count": 0,
            "escalation_candidate": True,
        }

    @staticmethod
    def opinion():
        return {
            "choices": [{
                "message": {
                    "content": '{"verdict":"agree","confidence":"high","analysis":"ok","recommended_next_check":"verify"}'
                }
            }]
        }

    def test_known_v082_defaults_migrate_to_groq_gpt_oss(self):
        cfg = normalize_expert_config({
            "provider_order": ["cerebras", "groq"],
            "free_api": {"groq": {"model": "qwen/qwen3.6-27b"}},
        })
        self.assertEqual(cfg["provider_order"], ["groq", "cerebras"])
        self.assertEqual(cfg["free_api"]["groq"]["model"], "openai/gpt-oss-120b")
        self.assertTrue(cfg["circuit_breaker"]["enabled"])

    def test_custom_provider_order_is_preserved(self):
        cfg = normalize_expert_config({
            "provider_order": ["cerebras"],
            "free_api": {"groq": {"model": "custom/model"}},
        })
        self.assertEqual(cfg["provider_order"], ["cerebras"])
        self.assertEqual(cfg["free_api"]["groq"]["model"], "custom/model")

    def test_groq_is_primary_and_cerebras_is_not_called_after_success(self):
        with tempfile.TemporaryDirectory() as td:
            old_g = os.environ.get("GROQ_API_KEY")
            old_c = os.environ.get("CEREBRAS_API_KEY")
            os.environ["GROQ_API_KEY"] = "groq-test-key"
            os.environ["CEREBRAS_API_KEY"] = "cerebras-test-key"
            try:
                service = ExpertEscalation(
                    {
                        "provider_order": ["cerebras", "groq"],
                        "free_api": {"groq": {"model": "qwen/qwen3.6-27b"}},
                    },
                    db_path=Path(td) / "expert.db",
                )
                seen = []

                def fake_post(endpoint, headers, payload, timeout):
                    seen.append(endpoint)
                    self.assertIn("api.groq.com", endpoint)
                    self.assertEqual(payload["model"], "openai/gpt-oss-120b")
                    return self.opinion()

                service._post_json = fake_post
                result = service.ask_free("problema", "respuesta", self.assessment())
                self.assertTrue(result["ok"])
                self.assertEqual(result["provider"], "groq")
                self.assertEqual(result["model"], "openai/gpt-oss-120b")
                self.assertEqual(len(seen), 1)
            finally:
                if old_g is None:
                    os.environ.pop("GROQ_API_KEY", None)
                else:
                    os.environ["GROQ_API_KEY"] = old_g
                if old_c is None:
                    os.environ.pop("CEREBRAS_API_KEY", None)
                else:
                    os.environ["CEREBRAS_API_KEY"] = old_c

    def test_payment_required_opens_persistent_cerebras_circuit(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("CEREBRAS_API_KEY")
            os.environ["CEREBRAS_API_KEY"] = "cerebras-test-key"
            db = Path(td) / "expert.db"
            try:
                service = ExpertEscalation(
                    {"provider_order": ["cerebras"]},
                    db_path=db,
                )
                calls = []

                def payment_required(*args, **kwargs):
                    calls.append(1)
                    raise ProviderHTTPError(402)

                service._post_json = payment_required
                first = service.ask_free(
                    "problema", "respuesta", self.assessment(), force_provider="cerebras"
                )
                self.assertFalse(first["ok"])
                self.assertEqual(first["error"], "http_402")
                self.assertEqual(first["http_status"], 402)
                self.assertTrue(first["circuit_opened"])
                self.assertEqual(first["circuit_reason"], "payment_required")
                self.assertEqual(len(calls), 1)

                # Reabrir el servicio con la misma DB debe conservar el circuito.
                service2 = ExpertEscalation(
                    {"provider_order": ["cerebras"]},
                    db_path=db,
                )
                service2._post_json = lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("No debe tocar la red con el circuito abierto")
                )
                second = service2.ask_free(
                    "problema", "respuesta", self.assessment(), force_provider="cerebras"
                )
                self.assertFalse(second["ok"])
                self.assertEqual(second["error"], "provider_circuit_open")
                self.assertEqual(second["http_status"], 402)
                self.assertGreater(second["retry_after_seconds"], 0)
                self.assertTrue(service2.status()["providers"]["cerebras"]["circuit_open"])
            finally:
                if old is None:
                    os.environ.pop("CEREBRAS_API_KEY", None)
                else:
                    os.environ["CEREBRAS_API_KEY"] = old

    def test_server_error_on_groq_falls_back_to_cerebras(self):
        with tempfile.TemporaryDirectory() as td:
            old_g = os.environ.get("GROQ_API_KEY")
            old_c = os.environ.get("CEREBRAS_API_KEY")
            os.environ["GROQ_API_KEY"] = "groq-test-key"
            os.environ["CEREBRAS_API_KEY"] = "cerebras-test-key"
            try:
                service = ExpertEscalation({}, db_path=Path(td) / "expert.db")
                seen = []

                def fake_post(endpoint, headers, payload, timeout):
                    seen.append(endpoint)
                    if "api.groq.com" in endpoint:
                        raise ProviderHTTPError(503)
                    return self.opinion()

                service._post_json = fake_post
                result = service.ask_free("problema", "respuesta", self.assessment())
                self.assertTrue(result["ok"])
                self.assertEqual(result["provider"], "cerebras")
                self.assertEqual(len(seen), 2)
                self.assertEqual(result["attempts"][0]["error"], "http_503")
                self.assertTrue(service.provider_health("groq")["open"])
            finally:
                if old_g is None:
                    os.environ.pop("GROQ_API_KEY", None)
                else:
                    os.environ["GROQ_API_KEY"] = old_g
                if old_c is None:
                    os.environ.pop("CEREBRAS_API_KEY", None)
                else:
                    os.environ["CEREBRAS_API_KEY"] = old_c

    def test_architecture_marks_resilience_native(self):
        self.assertIn("expert_resilience", architecture_status()["github_managed_native"])


if __name__ == "__main__":
    unittest.main()
