from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assistant.profiler import PerformanceProfiler
from assistant.self_repair import SelfRepairManager
from assistant.agent_diagnostics import performance_direct_intent
import assistant.config as config_mod


class PerformanceProfilerTests(unittest.TestCase):
    def test_records_and_summarizes_locally(self):
        with tempfile.TemporaryDirectory() as td:
            profiler = PerformanceProfiler(
                Path(td) / "performance.db",
                {"enabled": True, "slow_ms": 100, "max_events": 200},
            )
            profiler.record("agent.total", 250.0, True, {"route": "direct", "prompt": "secret text"})
            profiler.record("agent.total", 150.0, True)
            profiler.record("tool.system_status", 20.0, True)
            report = profiler.summary(hours=24)
            rows = {x["operation"]: x for x in report["operations"]}
            self.assertEqual(rows["agent.total"]["calls"], 2)
            self.assertAlmostEqual(float(rows["agent.total"]["avg_ms"]), 200.0, places=1)
            self.assertTrue(any(x["operation"] == "agent.total" for x in report["slow_operations"]))
            recent = profiler.recent(3)
            route_event = next(x for x in recent if x["operation"] == "agent.total" and x["metadata"])
            self.assertEqual(route_event["metadata"].get("route"), "direct")
            self.assertNotIn("prompt", route_event["metadata"])


class SelfRepairTests(unittest.TestCase):
    def test_proposes_known_repairs_without_executing_them(self):
        config = {
            "model": "qwen3.5:4b",
            "semantic_memory": {"model": "qwen3-embedding:0.6b"},
        }
        manager = SelfRepairManager(config)
        report = {
            "checks": [
                {"name": "Core Nova", "status": "error", "detail": "Faltan: app.py"},
                {"name": "Dependencias", "status": "warn", "detail": "Faltan módulos: playwright"},
                {"name": "Ollama", "status": "warn", "detail": "Conectado, pero no aparece qwen3.5:4b"},
                {
                    "name": "Semantic Memory",
                    "status": "warn",
                    "detail": "Falta qwen3-embedding:0.6b",
                    "semantic": {"model": "qwen3-embedding:0.6b", "model_available": False},
                },
            ]
        }
        ids = {x["id"] for x in manager.available_actions(report)}
        self.assertIn("repair_current_release", ids)
        self.assertIn("install_requirements", ids)
        self.assertIn("pull_main_model", ids)
        self.assertIn("pull_semantic_model", ids)


class ConfigMigrationTests(unittest.TestCase):
    def test_old_ctrl_space_hotkey_is_migrated(self):
        old_path = config_mod.CONFIG_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "config.json"
                path.write_text(json.dumps({"hotkey": "<ctrl>+<space>"}), encoding="utf-8")
                config_mod.CONFIG_PATH = path
                cfg = config_mod.load_config()
                self.assertEqual(cfg["hotkey"], "<ctrl>+<alt>+<space>")
                persisted = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["hotkey"], "<ctrl>+<alt>+<space>")
        finally:
            config_mod.CONFIG_PATH = old_path

    def test_custom_hotkey_is_preserved(self):
        old_path = config_mod.CONFIG_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "config.json"
                path.write_text(json.dumps({"hotkey": "<f8>"}), encoding="utf-8")
                config_mod.CONFIG_PATH = path
                cfg = config_mod.load_config()
                self.assertEqual(cfg["hotkey"], "<f8>")
        finally:
            config_mod.CONFIG_PATH = old_path


class RoutingTests(unittest.TestCase):
    def test_performance_and_hotkey_intents(self):
        self.assertEqual(performance_direct_intent("Nova, ¿cómo va tu rendimiento?"), "performance")
        self.assertEqual(performance_direct_intent("¿Qué puedes reparar?"), "repairs")
        self.assertEqual(performance_direct_intent("¿Cuál es tu atajo global?"), "hotkey")
        self.assertIsNone(performance_direct_intent("abre Minecraft"))


if __name__ == "__main__":
    unittest.main()
