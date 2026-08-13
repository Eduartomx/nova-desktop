from __future__ import annotations

import unittest
from unittest.mock import patch

from assistant.agent_gaming import gaming_direct_intent
from assistant.gaming_awareness import GamingAwarenessManager
from assistant.llm_warm import LLMWarmManager


class FakeWarm:
    def __init__(self, loaded=True, size_vram_mb=3200.0):
        self.loaded = loaded
        self.size_vram_mb = size_vram_mb
        self.preload_on_start = True
        self.suppressed = ""
        self.override = None
        self.override_reason = ""
        self.last_unload_reason = ""
        self.unload_calls = 0
        self.preload_calls = 0

    def cached_status(self):
        return {
            "loaded": self.loaded,
            "warming": False,
            "active_inferences": 0,
            "size_vram_mb": self.size_vram_mb if self.loaded else 0.0,
            "last_unload_reason": self.last_unload_reason,
        }

    def status(self, refresh=True):
        del refresh
        return self.cached_status()

    def suppress_preload(self, reason="runtime"):
        self.suppressed = reason

    def clear_preload_suppression(self, reason=None):
        if reason is None or self.suppressed == reason:
            self.suppressed = ""

    def set_runtime_keep_alive_override(self, value, reason="runtime"):
        self.override = value
        self.override_reason = reason

    def clear_runtime_keep_alive_override(self, reason=None):
        if reason is None or self.override_reason == reason:
            self.override = None
            self.override_reason = ""

    def unload(self, timeout=None, reason="manual", force=False):
        del timeout, force
        self.unload_calls += 1
        self.loaded = False
        self.last_unload_reason = reason
        return self.cached_status()

    def preload(self, reason="manual"):
        self.preload_calls += 1
        if self.suppressed:
            out = self.cached_status()
            out["preload_skipped_reason"] = self.suppressed
            return out
        self.loaded = True
        return self.cached_status()

    def start_background(self, callback=None, reason="startup"):
        report = self.preload(reason=reason)
        if callback:
            callback(report)
        return None


class FakePerception:
    def __init__(self, app_kind="game", process="game.exe"):
        self.app_kind = app_kind
        self.process = process
        self.runtime_poll = None
        self.config = {"poll_interval_ms": 1100}

    def current(self, refresh=False):
        del refresh
        if not self.process:
            return {"external": {}}
        return {
            "external": {
                "pid": 123,
                "process": self.process,
                "title": "Juego de prueba",
                "app_kind": self.app_kind,
            }
        }

    def set_runtime_poll_interval_ms(self, value):
        self.runtime_poll = value
        return self.effective_poll_interval_ms()

    def effective_poll_interval_ms(self):
        return int(self.runtime_poll or self.config["poll_interval_ms"])


class GamingAwarenessTests(unittest.TestCase):
    def _config(self, **overrides):
        gaming = {
            "enabled": True,
            "auto_detect": True,
            "poll_seconds": 1,
            "enter_dwell_seconds": 0,
            "exit_dwell_seconds": 0,
            "release_policy": "smart",
            "auto_release_llm": True,
            "keep_llm_loaded_during_game": False,
            "llm_keep_alive_during_game": 0,
            "vram_release_percent": 65.0,
            "vram_min_free_mb": 2600.0,
            "restore_preload_after_game": False,
            "perception_poll_ms_during_game": 2500,
            "game_processes": [],
            "game_path_markers": [],
            "minecraft_command_markers": [],
        }
        gaming.update(overrides)
        return {"model": "qwen3.5:4b", "gaming_awareness": gaming}

    def test_foreground_game_releases_qwen_and_throttles_perception(self):
        warm = FakeWarm(loaded=True)
        perception = FakePerception()
        manager = GamingAwarenessManager(
            self._config(),
            warm_manager=warm,
            perception=perception,
            gpu_sensor=lambda: {"utilization": 40, "vram_used_mb": 6700, "vram_total_mb": 8188},
            process_sensor=lambda: None,
        )
        report = manager.tick()
        self.assertTrue(report["active"])
        self.assertTrue(report["llm_released"])
        self.assertEqual(warm.unload_calls, 1)
        self.assertEqual(warm.last_unload_reason, "gaming_mode")
        self.assertEqual(warm.override, 0)
        self.assertEqual(warm.suppressed, "gaming_mode")
        self.assertEqual(perception.runtime_poll, 2500)

    def test_exit_restores_runtime_policies(self):
        warm = FakeWarm(loaded=True)
        perception = FakePerception()
        manager = GamingAwarenessManager(
            self._config(),
            warm_manager=warm,
            perception=perception,
            gpu_sensor=lambda: {"utilization": 20, "vram_used_mb": 6000, "vram_total_mb": 8188},
            process_sensor=lambda: None,
        )
        manager.tick()
        perception.process = ""
        report = manager.tick()
        self.assertFalse(report["active"])
        self.assertEqual(warm.suppressed, "")
        self.assertIsNone(warm.override)
        self.assertIsNone(perception.runtime_poll)

    def test_user_can_keep_qwen_loaded_during_games(self):
        warm = FakeWarm(loaded=True)
        perception = FakePerception()
        manager = GamingAwarenessManager(
            self._config(keep_llm_loaded_during_game=True),
            warm_manager=warm,
            perception=perception,
            gpu_sensor=lambda: {"utilization": 90, "vram_used_mb": 7900, "vram_total_mb": 8188},
            process_sensor=lambda: None,
        )
        report = manager.tick()
        self.assertTrue(report["active"])
        self.assertEqual(warm.unload_calls, 0)
        self.assertTrue(warm.loaded)
        self.assertIsNone(warm.override)

    def test_manual_off_overrides_detected_game(self):
        warm = FakeWarm(loaded=True)
        perception = FakePerception()
        manager = GamingAwarenessManager(
            self._config(), warm_manager=warm, perception=perception,
            gpu_sensor=lambda: None, process_sensor=lambda: None,
        )
        manager.set_mode("off")
        report = manager.tick()
        self.assertFalse(report["active"])
        self.assertEqual(report["mode"], "off")


class GamingIntentTests(unittest.TestCase):
    def test_direct_commands(self):
        self.assertEqual(gaming_direct_intent("Nova, activa modo juego"), "on")
        self.assertEqual(gaming_direct_intent("Nova, desactiva modo juego"), "off")
        self.assertEqual(gaming_direct_intent("Vuelve a modo juego automático"), "auto")
        self.assertEqual(gaming_direct_intent("Mantén Qwen cargado aunque esté jugando"), "keep_llm")
        self.assertEqual(gaming_direct_intent("Libera Qwen cuando juegue"), "release_llm")
        self.assertEqual(gaming_direct_intent("¿Por qué liberaste Qwen?"), "status")
        self.assertIsNone(gaming_direct_intent("Abre el navegador"))


class WarmGamingCoordinationTests(unittest.TestCase):
    def _manager(self):
        return LLMWarmManager({
            "model": "qwen3.5:4b",
            "ollama_host": "http://127.0.0.1:11434",
            "llm_warm": {"enabled": True, "preload_on_start": True, "keep_alive": "20m"},
        })

    def test_runtime_keep_alive_override_is_applied(self):
        manager = self._manager()
        manager.set_runtime_keep_alive_override(0, reason="gaming_mode")
        payload = {"model": "qwen3.5:4b"}
        manager.apply_keep_alive(payload)
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(manager.cached_status()["runtime_keep_alive_reason"], "gaming_mode")
        manager.clear_runtime_keep_alive_override("gaming_mode")
        manager.apply_keep_alive(payload)
        self.assertEqual(payload["keep_alive"], "20m")

    @patch("assistant.llm_warm.requests.post")
    def test_preload_is_suppressed_and_unload_deferred_during_inference(self, post):
        manager = self._manager()
        manager.suppress_preload("gaming_mode")
        report = manager.preload()
        self.assertEqual(report.get("preload_skipped_reason"), "gaming_mode")
        self.assertFalse(post.called)

        manager.clear_preload_suppression("gaming_mode")
        manager._last_loaded = True
        manager.begin_inference()
        report = manager.unload(reason="gaming_mode")
        self.assertTrue(report.get("unload_deferred"))
        self.assertEqual(report.get("unload_deferred_reason"), "active_inference")
        self.assertFalse(post.called)
        manager.end_inference()


if __name__ == "__main__":
    unittest.main()
