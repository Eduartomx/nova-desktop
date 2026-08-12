from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.agent_skills import skills_direct_intent
from assistant.skills import SkillRegistry


class FakeMemory:
    def __init__(self, workspace_id=None):
        self.workspace_id = workspace_id

    def active_workspace(self):
        if self.workspace_id is None:
            return None
        return {"id": self.workspace_id, "name": f"WS-{self.workspace_id}"}


class SkillsEngineTests(unittest.TestCase):
    def make_registry(self, td, memory=None, **cfg):
        return SkillRegistry(
            config={"max_steps": 12, "suggest_threshold": 0.7, **cfg},
            memory=memory,
            db_path=Path(td) / "skills.db",
        )

    @staticmethod
    def definition():
        return dict(
            name="Reiniciar servidor de prueba",
            description="Detiene de forma controlada un servidor de prueba, lo inicia y verifica que responda.",
            triggers=["reinicia el servidor de prueba", "levanta el servidor de prueba"],
            parameters={
                "server": {"type": "string", "required": True, "description": "Nombre del servidor"},
                "wait": {"type": "integer", "required": False, "default": 5},
            },
            steps=[
                {
                    "title": "Comprobar estado",
                    "instruction": "Comprueba el estado de {server} antes de cambiar nada.",
                    "tool_hint": "system_status",
                    "verify": "Registrar si {server} estaba activo.",
                },
                {
                    "title": "Reiniciar",
                    "instruction": "Reinicia {server} y espera {wait} segundos.",
                    "tool_hint": "task_engine",
                    "verify": "El proceso nuevo debe quedar activo.",
                },
            ],
            verification=["{server} responde después del reinicio."],
            permissions=["process_control"],
        )

    def test_save_compile_and_run_are_declarative(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self.make_registry(td)
            skill = registry.save(**self.definition(), source="user")
            self.assertEqual(skill["version"], 1)
            self.assertEqual(skill["trust_level"], "user")
            self.assertTrue(registry.status()["declarative_only"])
            self.assertTrue(registry.status()["inherits_security_policy"])

            compiled = registry.compile(skill, {"server": "Alpha"})
            self.assertEqual(compiled.missing, [])
            self.assertIn("Alpha", compiled.steps[0]["instruction"])
            self.assertIn("5", compiled.steps[1]["instruction"])
            run_id = registry.start_run(compiled)
            playbook = registry.format_playbook(compiled, run_id)
            self.assertIn("NO un permiso", playbook)
            self.assertIn("Alpha", playbook)
            self.assertEqual(registry.run_info(run_id)["status"], "prepared")

    def test_missing_required_parameter_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self.make_registry(td)
            skill = registry.save(**self.definition())
            compiled = registry.compile(skill, {})
            self.assertEqual(compiled.missing, ["server"])
            with self.assertRaises(ValueError):
                registry.start_run(compiled)

    def test_update_creates_revision_and_increments_version(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self.make_registry(td)
            first = registry.save(**self.definition(), source="user")
            data = self.definition()
            data["description"] = "Versión actualizada"
            second = registry.save(**data, source="user")
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["version"], 2)
            revisions = registry.revisions(second["id"])
            self.assertEqual(len(revisions), 1)
            self.assertEqual(revisions[0]["version"], 1)

    def test_workspace_scope_wins_and_global_remains_available(self):
        with tempfile.TemporaryDirectory() as td:
            memory = FakeMemory(7)
            registry = self.make_registry(td, memory=memory)
            global_skill = registry.save(**self.definition(), source="user")
            local = self.definition()
            local["description"] = "Variante específica del proyecto"
            local_skill = registry.save(**local, source="user", workspace_id=7)
            resolved = registry.get("Reiniciar servidor de prueba")
            self.assertEqual(resolved["id"], local_skill["id"])
            rows = registry.list()
            self.assertEqual({x["id"] for x in rows}, {global_skill["id"], local_skill["id"]})

    def test_matching_uses_triggers_without_auto_execution(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self.make_registry(td)
            registry.save(**self.definition())
            matches = registry.match("Nova, reinicia el servidor de prueba ahora")
            self.assertTrue(matches)
            self.assertGreaterEqual(matches[0]["match_score"], 0.78)
            self.assertFalse(registry.status()["auto_execute_matches"])

    def test_secret_material_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self.make_registry(td)
            data = self.definition()
            data["steps"] = [{"instruction": "Usa api_key=sk-1234567890abcdefghijklmnop para continuar"}]
            with self.assertRaises(ValueError):
                registry.save(**data)

    def test_sensitive_run_arguments_never_enter_playbook_or_storage(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self.make_registry(td)
            data = self.definition()
            data["parameters"]["password"] = {"type": "string", "required": False}
            data["steps"][1]["instruction"] += " Credencial: {password}."
            skill = registry.save(**data)
            compiled = registry.compile(skill, {"server": "Alpha", "password": "super-secret-value"})
            self.assertIn("password", compiled.sensitive_parameters)
            self.assertIn("[SENSITIVE_PARAMETER:password]", compiled.steps[1]["instruction"])
            self.assertNotIn("super-secret-value", registry.format_playbook(compiled))
            run_id = registry.start_run(compiled)
            info = registry.run_info(run_id)
            self.assertEqual(info["arguments"]["password"], "[REDACTED]")
            self.assertNotIn("super-secret-value", str(info))
            self.assertNotIn("[SENSITIVE_PARAMETER:password]", str(info["steps"]))
            self.assertFalse(registry.status()["persist_run_summaries"])

    def test_finish_run_is_idempotent_and_promotes_after_two_distinct_successes(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self.make_registry(td)
            skill = registry.save(**self.definition(), source="nova")
            self.assertEqual(skill["trust_level"], "draft")

            compiled = registry.compile(skill, {"server": "Alpha"})
            first_id = registry.start_run(compiled)
            registry.finish_run(first_id, True, "verificado")
            registry.finish_run(first_id, True, "no debe contar dos veces")
            skill = registry.get(skill["id"])
            self.assertEqual(skill["successful_runs"], 1)
            self.assertEqual(skill["trust_level"], "draft")

            compiled = registry.compile(skill, {"server": "Alpha"})
            second_id = registry.start_run(compiled)
            registry.finish_run(second_id, True, "verificado")
            skill = registry.get(skill["id"])
            self.assertEqual(skill["successful_runs"], 2)
            self.assertEqual(skill["trust_level"], "verified")

    def test_direct_routing(self):
        self.assertEqual(skills_direct_intent("Nova, ¿qué habilidades tienes?"), "list")
        self.assertEqual(skills_direct_intent("Estado de habilidades"), "status")
        self.assertEqual(skills_direct_intent("Usa la habilidad reiniciar servidor"), "run")
        self.assertIsNone(skills_direct_intent("abre el navegador"))


if __name__ == "__main__":
    unittest.main()
