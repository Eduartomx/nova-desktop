from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.agent_expert import expert_direct_intent
from assistant.config import DEFAULT_CONFIG
from assistant.core_runtime import architecture_status
from assistant.expert_escalation import ExpertEscalation, redact_secrets


class ExpertEscalationTests(unittest.TestCase):
    @staticmethod
    def assessment(risk="normal", candidate=True):
        return {
            "request_kind": "diagnosis",
            "risk_level": risk,
            "score": 0.41,
            "band": "low",
            "evidence_count": 1,
            "verification_count": 0,
            "failure_count": 1,
            "contradiction_count": 0,
            "escalation_candidate": candidate,
            "reason_codes": ["tool_failures"],
        }

    def make_service(self, td, **cfg):
        base = {
            "enabled": True,
            "auto_free_second_opinion": True,
            "provider_order": ["cerebras", "groq"],
            "chatgpt_assisted": {"enabled": True, "open_browser": True, "copy_query_to_clipboard": True},
            **cfg,
        }
        return ExpertEscalation(base, db_path=Path(td) / "expert.db")

    def test_secret_redaction_and_packet_minimization(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            secret = "sk-super-private-1234567890abcdef"
            packet = service.build_packet(
                f"El error ocurre con api_key={secret}",
                "Mi password=hunter2 y bearer abcdefghijklmnopqrstuvwxyz",
                self.assessment(),
            )
            self.assertNotIn(secret, packet["text"])
            self.assertNotIn("hunter2", packet["text"])
            self.assertIn("[REDACTED]", packet["text"])
            self.assertEqual(len(packet["sha256"]), 64)
            self.assertNotIn(secret, redact_secrets(f"token={secret}"))

    def test_free_provider_response_and_db_do_not_persist_contents(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            old = os.environ.get("CEREBRAS_API_KEY")
            os.environ["CEREBRAS_API_KEY"] = "ENV_KEY_MUST_NOT_PERSIST"
            try:
                def fake_post(endpoint, headers, payload, timeout):
                    self.assertIn("cerebras.ai", endpoint)
                    self.assertIn("Bearer ", headers["Authorization"])
                    return {
                        "choices": [{
                            "message": {
                                "content": '{"verdict":"partially_agree","confidence":"medium","analysis":"EXTERNAL_PRIVATE_ANSWER","recommended_next_check":"revisa el log"}'
                            }
                        }]
                    }

                service._post_json = fake_post
                result = service.ask_free(
                    "UNIQUE_PRIVATE_PROBLEM password=DO_NOT_STORE_ME",
                    "UNIQUE_PRIVATE_LOCAL_ANSWER",
                    self.assessment(),
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["provider"], "cerebras")
                self.assertEqual(result["verdict"], "partially_agree")
                self.assertIn("EXTERNAL_PRIVATE_ANSWER", result["analysis"])

                raw = Path(td, "expert.db").read_bytes()
                for forbidden in (
                    b"UNIQUE_PRIVATE_PROBLEM",
                    b"UNIQUE_PRIVATE_LOCAL_ANSWER",
                    b"EXTERNAL_PRIVATE_ANSWER",
                    b"DO_NOT_STORE_ME",
                    b"ENV_KEY_MUST_NOT_PERSIST",
                ):
                    self.assertNotIn(forbidden, raw)
                with sqlite3.connect(Path(td) / "expert.db") as conn:
                    row = conn.execute("SELECT method,provider,status,payload_chars,response_chars FROM expert_events").fetchone()
                self.assertEqual(row[0], "free_api")
                self.assertEqual(row[1], "cerebras")
                self.assertEqual(row[2], "success")
                self.assertGreater(row[3], 0)
                self.assertGreater(row[4], 0)
            finally:
                if old is None:
                    os.environ.pop("CEREBRAS_API_KEY", None)
                else:
                    os.environ["CEREBRAS_API_KEY"] = old

    def test_groq_is_fallback_when_cerebras_key_missing(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            old_c = os.environ.pop("CEREBRAS_API_KEY", None)
            old_g = os.environ.get("GROQ_API_KEY")
            os.environ["GROQ_API_KEY"] = "groq-test-key"
            seen = []
            try:
                def fake_post(endpoint, headers, payload, timeout):
                    seen.append(endpoint)
                    return {"choices": [{"message": {"content": '{"verdict":"agree","confidence":"high","analysis":"ok","recommended_next_check":"verify"}'}}]}

                service._post_json = fake_post
                result = service.ask_free("problema", "respuesta", self.assessment())
                self.assertTrue(result["ok"])
                self.assertEqual(result["provider"], "groq")
                self.assertEqual(len(seen), 1)
                self.assertIn("api.groq.com", seen[0])
                self.assertEqual(result["attempts"][0]["error"], "api_key_missing")
            finally:
                if old_c is not None:
                    os.environ["CEREBRAS_API_KEY"] = old_c
                if old_g is None:
                    os.environ.pop("GROQ_API_KEY", None)
                else:
                    os.environ["GROQ_API_KEY"] = old_g

    def test_auto_external_never_runs_for_high_or_critical_risk(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            self.assertTrue(service.should_auto_free(self.assessment("normal", True)))
            self.assertFalse(service.should_auto_free(self.assessment("high", True)))
            self.assertFalse(service.should_auto_free(self.assessment("critical", True)))
            self.assertFalse(service.should_auto_free(self.assessment("normal", False)))

    def test_chatgpt_assisted_only_prepares_and_opens(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            service.remember_candidate("problema", "respuesta local", self.assessment())
            service._clipboard_write = lambda text: (True, "")
            service._post_json = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No debe llamar una API"))
            with patch("assistant.expert_escalation.webbrowser.open", return_value=True) as opened:
                result = service.prepare_chatgpt(trigger="test")
            self.assertTrue(result["ok"])
            self.assertTrue(result["copied"])
            self.assertTrue(result["browser_opened"])
            opened.assert_called_once()
            self.assertIn("envíala manualmente", result["instructions"])

    def test_imported_chatgpt_response_is_memory_only(self):
        with tempfile.TemporaryDirectory() as td:
            service = self.make_service(td)
            service.remember_candidate("problema original", "respuesta local", self.assessment())
            imported = service.import_chatgpt_response("CHATGPT_PRIVATE_RESPONSE con una comprobación concreta.")
            self.assertTrue(imported["ok"])
            context = service.imported_context()
            self.assertIn("CHATGPT_PRIVATE_RESPONSE", context)
            self.assertIn("NO CONFIABLE", context)
            raw = Path(td, "expert.db").read_bytes()
            self.assertNotIn(b"CHATGPT_PRIVATE_RESPONSE", raw)
            self.assertNotIn(b"problema original", raw)

    def test_direct_routing(self):
        self.assertEqual(expert_direct_intent("Nova, estado del experto"), "status")
        self.assertEqual(expert_direct_intent("Consulta la API gratuita"), "free")
        self.assertEqual(expert_direct_intent("Pregunta a ChatGPT"), "prepare_chatgpt")
        self.assertEqual(expert_direct_intent("Importa la respuesta de ChatGPT"), "import_chatgpt")
        self.assertIsNone(expert_direct_intent("abre el explorador de archivos"))

    def test_config_disables_paid_openai_and_declares_free_expert(self):
        self.assertFalse(DEFAULT_CONFIG["openai"]["enabled"])
        self.assertFalse(DEFAULT_CONFIG["openai"]["paid_api_opt_in"])
        expert = DEFAULT_CONFIG["expert_escalation"]
        self.assertEqual(expert["provider_order"][0], "cerebras")
        self.assertEqual(expert["free_api"]["cerebras"]["api_key_env"], "CEREBRAS_API_KEY")
        self.assertEqual(expert["free_api"]["groq"]["api_key_env"], "GROQ_API_KEY")

    def test_core_contract_and_no_paid_openai_endpoint(self):
        status = architecture_status()
        self.assertIn("expert_escalation", status["github_managed_native"])
        source = (Path(__file__).resolve().parents[1] / "nova" / "assistant" / "expert_escalation.py").read_text(encoding="utf-8")
        self.assertNotIn("api.openai.com", source)
        self.assertNotIn("OPENAI_API_KEY", source)


if __name__ == "__main__":
    unittest.main()
