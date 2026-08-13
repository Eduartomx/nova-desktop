from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.agent_confidence import confidence_direct_intent, format_assessment
from assistant.confidence import ConfidenceEngine, classify_request
from assistant.config import DEFAULT_CONFIG
from assistant.core_runtime import architecture_status


class ConfidenceEngineTests(unittest.TestCase):
    def make_engine(self, td, **config):
        return ConfidenceEngine(
            config={
                "persist_assessments": True,
                "low_threshold": 0.52,
                "high_threshold": 0.78,
                "escalation_candidate_threshold": 0.50,
                **config,
            },
            db_path=Path(td) / "confidence.db",
        )

    def test_diagnosis_without_evidence_is_low_and_escalation_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            engine = self.make_engine(td)
            engine.begin_request("¿Por qué crashea este programa?")
            result = engine.finish_request(response_ok=True)
            self.assertEqual(result["request_kind"], "diagnosis")
            self.assertEqual(result["band"], "low")
            self.assertTrue(result["escalation_candidate"])
            self.assertIn("no_structured_evidence", result["reason_codes"])
            self.assertTrue(result["heuristic_not_probability"])

    def test_verified_structured_evidence_can_reach_high_confidence(self):
        with tempfile.TemporaryDirectory() as td:
            engine = self.make_engine(td)
            result = engine.manual_assess(
                request_kind="diagnosis",
                structured_reads=3,
                verifications=2,
                skill_trust="verified",
            )
            self.assertEqual(result["band"], "high")
            self.assertGreaterEqual(result["score"], 0.78)
            self.assertFalse(result["escalation_candidate"])

    def test_failures_and_contradictions_reduce_confidence(self):
        with tempfile.TemporaryDirectory() as td:
            engine = self.make_engine(td)
            baseline = engine.manual_assess(request_kind="factual", structured_reads=2, verifications=1)
            degraded = engine.manual_assess(
                request_kind="factual", structured_reads=2, verifications=1,
                failures=2, contradictions=1,
            )
            self.assertLess(degraded["score"], baseline["score"])
            self.assertTrue(degraded["escalation_candidate"])
            self.assertIn("contradictions", degraded["reason_codes"])

    def test_critical_request_is_capped_without_verification(self):
        with tempfile.TemporaryDirectory() as td:
            engine = self.make_engine(td)
            result = engine.manual_assess(
                request_kind="factual", risk_level="critical",
                structured_reads=6, verifications=0, deterministic=True,
            )
            self.assertLessEqual(result["score"], 0.54)
            self.assertTrue(result["escalation_candidate"])

    def test_tool_sequence_treats_read_after_action_as_verification(self):
        with tempfile.TemporaryDirectory() as td:
            engine = self.make_engine(td)
            engine.begin_request("abre el programa y comprueba que quedó abierto")
            engine.record_tool("open_app", {"ok": True})
            engine.record_tool("process_status", {"ok": True, "running": True})
            result = engine.finish_request()
            self.assertGreaterEqual(result["verification_count"], 1)
            self.assertIn("process_status", result["tool_names"])

    def test_prompts_responses_and_tool_outputs_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            engine = self.make_engine(td)
            secret = "super-secret-value-DO-NOT-PERSIST"
            engine.begin_request(f"diagnostica esto pero mi token es {secret}")
            engine.record_tool("system_status", {"ok": True, "detail": secret})
            engine.finish_request()
            raw = (Path(td) / "confidence.db").read_bytes()
            self.assertNotIn(secret.encode("utf-8"), raw)
            status = engine.status()
            self.assertFalse(status["persists_prompts"])
            self.assertFalse(status["persists_responses"])
            self.assertFalse(status["score_is_calibrated_probability"])

    def test_classification_and_direct_routing(self):
        kind, risk = classify_request("¿Por qué falla el programa?")
        self.assertEqual(kind, "diagnosis")
        self.assertEqual(risk, "normal")
        _, risk2 = classify_request("Compra esto y paga con mi cuenta")
        self.assertEqual(risk2, "critical")
        self.assertEqual(confidence_direct_intent("Nova, ¿qué tan seguro estás?"), "last")
        self.assertEqual(confidence_direct_intent("Estado del Confidence Engine"), "status")
        self.assertEqual(confidence_direct_intent("Historial de confianza"), "recent")
        self.assertIsNone(confidence_direct_intent("abre el navegador"))

    def test_format_explicitly_says_score_is_not_probability(self):
        text = format_assessment({
            "score": 0.42, "band": "low", "evidence_count": 0,
            "failure_count": 1, "contradiction_count": 0,
            "reason_codes": ["tool_failures"], "escalation_candidate": True,
        })
        self.assertIn("NO es una probabilidad calibrada", text)
        self.assertIn("segunda opinión", text)

    def test_defaults_and_architecture_contract(self):
        cfg = DEFAULT_CONFIG["confidence"]
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["persist_assessments"])
        self.assertTrue(cfg["surface_low_confidence"])
        status = architecture_status()
        self.assertIn("confidence", status["github_managed_native"])


if __name__ == "__main__":
    unittest.main()
