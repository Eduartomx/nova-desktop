from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from assistant.agent_reliability import reliability_direct_intent
from assistant.core_runtime import architecture_status
from assistant.experience_reliability import (
    SkillReliability,
    install_skill_reliability_hooks,
)
from assistant.skills import SkillRegistry


install_skill_reliability_hooks()


class ExperienceReliabilityTests(unittest.TestCase):
    def make_pair(self, td, **config):
        registry = SkillRegistry({}, db_path=Path(td) / "skills.db")
        engine = SkillReliability(config, registry=registry, db_path=Path(td) / "reliability.db")
        engine.attach_registry(registry)
        return registry, engine

    @staticmethod
    def save_skill(registry, name="Diagnóstico fiable", trust="verified"):
        return registry.save(
            name=name,
            description="Procedimiento de prueba",
            triggers=[name],
            parameters={},
            steps=[{"title": "Revisar", "instruction": "Revisa el estado", "verify": "Estado comprobado"}],
            verification=["Confirmar resultado"],
            permissions=[],
            source="user",
            trust_level=trust,
        )

    @staticmethod
    def run(registry, skill, success, summary=""):
        compiled = registry.compile(skill)
        run_id = registry.start_run(compiled)
        registry.finish_run(run_id, success, summary)
        return run_id

    def test_verified_skill_is_demoted_after_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as td:
            registry, engine = self.make_pair(td, consecutive_failures_review=2)
            skill = self.save_skill(registry, trust="verified")
            self.run(registry, skill, False)
            self.run(registry, skill, False)
            state = engine.report(skill["id"])
            self.assertEqual(state["band"], "degraded")
            self.assertTrue(state["needs_review"])
            self.assertEqual(state["consecutive_failures"], 2)
            self.assertEqual(registry.get(skill["id"])["trust_level"], "draft")
            self.assertFalse(engine.status()["auto_disables_skills"])
            self.assertTrue(registry.get(skill["id"])["enabled"])

    def test_successful_recent_history_becomes_stable(self):
        with tempfile.TemporaryDirectory() as td:
            registry, engine = self.make_pair(td, minimum_runs=3, stable_threshold=0.78)
            skill = self.save_skill(registry, trust="draft")
            for _ in range(3):
                self.run(registry, skill, True)
            state = engine.report(skill["id"])
            self.assertEqual(state["band"], "stable")
            self.assertGreaterEqual(state["score"], 0.78)
            self.assertFalse(state["needs_review"])
            self.assertEqual(registry.get(skill["id"])["trust_level"], "verified")

    def test_new_skill_version_does_not_inherit_old_failure_score(self):
        with tempfile.TemporaryDirectory() as td:
            registry, engine = self.make_pair(td)
            skill = self.save_skill(registry, name="Skill versionada", trust="verified")
            self.run(registry, skill, False)
            updated = registry.save(
                name="Skill versionada",
                description="Procedimiento corregido",
                triggers=["Skill versionada"],
                parameters={},
                steps=[{"instruction": "Usa el procedimiento corregido"}],
                verification=["Comprobar corrección"],
                permissions=[],
                source="user",
                trust_level="draft",
            )
            state = engine.report(updated["id"])
            self.assertEqual(updated["version"], 2)
            self.assertEqual(state["skill_version"], 2)
            self.assertEqual(state["band"], "unproven")
            self.assertEqual(state["failures"], 0)

    def test_stale_state_is_detected_without_editing_skill(self):
        with tempfile.TemporaryDirectory() as td:
            registry, engine = self.make_pair(td, stale_days=1)
            skill = self.save_skill(registry, name="Skill antigua", trust="verified")
            run_id = self.run(registry, skill, True)
            old = time.time() - (3 * 86400)
            with engine._connect() as conn:
                conn.execute(
                    "UPDATE skill_reliability_events SET created_ts=?,updated_ts=? WHERE run_id=?",
                    (old, old, run_id),
                )
                conn.commit()
            state = engine.report(skill["id"])
            self.assertEqual(state["band"], "stale")
            self.assertTrue(state["needs_review"])
            self.assertTrue(registry.get(skill["id"])["enabled"])
            self.assertEqual(registry.get(skill["id"])["trust_level"], "draft")

    def test_reliability_db_never_persists_skill_or_output_content(self):
        with tempfile.TemporaryDirectory() as td:
            registry, engine = self.make_pair(td)
            unique_instruction = "PRIVATE_PLAYBOOK_CONTENT_98XQ"
            unique_output = "PRIVATE_TOOL_OUTPUT_71ZZ"
            skill = registry.save(
                name="Privacidad fiabilidad",
                description="Prueba",
                triggers=["privacidad fiabilidad"],
                parameters={},
                steps=[{"instruction": unique_instruction}],
                verification=["verificar"],
                permissions=[],
                source="user",
                trust_level="draft",
            )
            self.run(registry, skill, True, unique_output)
            raw = Path(td, "reliability.db").read_bytes()
            self.assertNotIn(unique_instruction.encode(), raw)
            self.assertNotIn(unique_output.encode(), raw)
            self.assertFalse(engine.status()["persists_content"])

    def test_finishing_same_run_twice_does_not_duplicate_reliability_event(self):
        with tempfile.TemporaryDirectory() as td:
            registry, engine = self.make_pair(td)
            skill = self.save_skill(registry, name="Idempotente", trust="draft")
            compiled = registry.compile(skill)
            run_id = registry.start_run(compiled)
            registry.finish_run(run_id, True)
            registry.finish_run(run_id, True)
            with engine._connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM skill_reliability_events WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            self.assertEqual(count, 1)
            state = engine.report(skill["id"])
            self.assertEqual(state["successes"], 1)

    def test_direct_routing_and_architecture_contract(self):
        self.assertEqual(reliability_direct_intent("estado de fiabilidad de skills"), "status")
        self.assertEqual(reliability_direct_intent("qué habilidades están fallando"), "review")
        self.assertEqual(reliability_direct_intent("fiabilidad de la habilidad Diagnóstico fiable"), "report")
        self.assertIsNone(reliability_direct_intent("abre el explorador"))
        self.assertIn("experience_reliability", architecture_status()["github_managed_native"])


if __name__ == "__main__":
    unittest.main()
