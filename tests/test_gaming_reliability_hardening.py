from __future__ import annotations

import threading
import time
import unittest

from assistant.gaming_awareness import GamingAwarenessManager
from assistant.gaming_detection_filters import install_gaming_detection_filters
from assistant.gaming_reliability import install_gaming_reliability


class FakePerception:
    def __init__(self, state=None):
        self.state = state or {"sampled_at": time.time(), "foreground": {}, "external": {}}
        self.runtime_poll = None

    def current(self, refresh=False):
        return self.state

    def set_runtime_poll_interval_ms(self, value):
        self.runtime_poll = value


class BlockingWarm:
    def __init__(self):
        self.loaded = True
        self.preload_on_start = True
        self.suppressed = ""
        self.override = None
        self.last_unload_reason = ""
        self.unload_calls = 0
        self.preload_calls = 0
        self.warming = False
        self.active_inferences = 0
        self.preload_started = threading.Event()
        self.release_preload = threading.Event()

    def cached_status(self):
        return {
            "loaded": self.loaded,
            "warming": self.warming,
            "active_inferences": self.active_inferences,
            "size_vram_mb": 3200.0 if self.loaded else 0.0,
            "last_unload_reason": self.last_unload_reason,
        }

    def status(self, refresh=True):
        return self.cached_status()

    def suppress_preload(self, reason="runtime"):
        self.suppressed = reason

    def clear_preload_suppression(self, reason=None):
        if reason is None or self.suppressed == reason:
            self.suppressed = ""

    def set_runtime_keep_alive_override(self, value, reason="runtime"):
        self.override = value

    def clear_runtime_keep_alive_override(self, reason=None):
        self.override = None

    def unload(self, timeout=None, reason="manual", force=False):
        if not force and (self.warming or self.active_inferences > 0):
            report = self.cached_status()
            report["unload_deferred"] = True
            return report
        self.unload_calls += 1
        self.loaded = False
        self.last_unload_reason = reason
        return self.cached_status()

    def preload(self, reason="manual"):
        self.preload_calls += 1
        if self.suppressed:
            report = self.cached_status()
            report["preload_skipped_reason"] = self.suppressed
            return report
        self.warming = True
        self.preload_started.set()
        if not self.release_preload.wait(2.0):
            self.warming = False
            raise AssertionError("preload bloqueado no liberado por la prueba")
        # Simula una petición HTTP que ya había pasado la comprobación de
        # supresión antes de que entrara el nuevo juego.
        self.loaded = True
        self.warming = False
        return self.cached_status()


class GamingReliabilityHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_gaming_detection_filters()
        install_gaming_reliability()

    def _config(self, **overrides):
        gaming = {
            "enabled": True,
            "auto_detect": True,
            "enter_dwell_seconds": 0,
            "exit_dwell_seconds": 0,
            "release_policy": "always",
            "auto_release_llm": True,
            "keep_llm_loaded_during_game": False,
            "llm_keep_alive_during_game": 0,
            "restore_preload_after_game": True,
            "restore_delay_seconds": 0,
            "perception_max_age_seconds": 1.0,
            "game_processes": ["game.exe", "wallpaper64.exe"],
            "game_path_markers": ["/steamapps/common/"],
            "ignored_game_processes": ["wallpaper64.exe"],
            "ignored_game_path_markers": ["/steamapps/common/wallpaper_engine/"],
            "minecraft_command_markers": ["minecraft", "forge", "fabric-loader"],
        }
        gaming.update(overrides)
        return {"model": "qwen3.5:4b", "gaming_awareness": gaming}

    @staticmethod
    def _identity(table):
        def sensor(pid):
            row = table.get(int(pid))
            return dict(row) if row else None
        return sensor

    def test_wallpaper_explicit_process_is_blocked_for_injected_sensor(self):
        live = {20: {
            "alive": True,
            "process": "wallpaper64.exe",
            "create_time": 1,
            "exe": "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe",
        }}
        manager = GamingAwarenessManager(
            self._config(),
            warm_manager=BlockingWarm(),
            perception=FakePerception(),
            process_sensor=lambda: {
                "pid": 20,
                "process": "wallpaper64.exe",
                "exe": "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe",
                "source": "process",
            },
            identity_sensor=self._identity(live),
            gpu_sensor=lambda: {},
        )
        self.assertFalse(manager.tick()["active"])

    def test_wallpaper_explicit_process_is_blocked_for_perception(self):
        state = {
            "sampled_at": time.time(),
            "foreground": {
                "pid": 21,
                "process": "wallpaper64.exe",
                "app_kind": "game",
                "exe": "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe",
                "is_nova": False,
            },
            "external": {},
        }
        live = {21: {
            "alive": True,
            "process": "wallpaper64.exe",
            "create_time": 2,
            "exe": "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe",
        }}
        manager = GamingAwarenessManager(
            self._config(),
            warm_manager=BlockingWarm(),
            perception=FakePerception(state),
            process_sensor=lambda: None,
            identity_sensor=self._identity(live),
            gpu_sensor=lambda: {},
        )
        self.assertFalse(manager.tick()["active"])

    def test_inflight_restore_is_compensated_after_new_game_enters(self):
        warm = BlockingWarm()
        live = {80: {"alive": True, "process": "game.exe", "create_time": 1}}
        current = {"pid": 80, "process": "game.exe", "source": "process"}
        manager = GamingAwarenessManager(
            self._config(game_processes=["game.exe"]),
            warm_manager=warm,
            perception=FakePerception(),
            process_sensor=lambda: dict(current) if current else None,
            identity_sensor=self._identity(live),
            gpu_sensor=lambda: {},
        )

        manager.tick()
        self.assertEqual(warm.unload_calls, 1)
        self.assertFalse(warm.loaded)

        current.clear()
        live.clear()
        manager.tick()
        restore_thread = manager._restore_timer
        self.assertIsNotNone(restore_thread)
        self.assertTrue(warm.preload_started.wait(1.0))

        live[81] = {"alive": True, "process": "game.exe", "create_time": 2}
        current.update({"pid": 81, "process": "game.exe", "source": "process"})
        report = manager.tick()
        self.assertTrue(report["active"])
        self.assertEqual(report["game"]["pid"], 81)
        self.assertEqual(warm.suppressed, "gaming_mode")
        self.assertEqual(warm.override, 0)

        warm.release_preload.set()
        restore_thread.join(2.0)
        self.assertFalse(restore_thread.is_alive())

        final = manager.status(refresh=False)
        self.assertTrue(final["active"])
        self.assertEqual(final["game"]["pid"], 81)
        self.assertFalse(warm.loaded)
        self.assertEqual(warm.suppressed, "gaming_mode")
        self.assertEqual(warm.override, 0)
        self.assertEqual(warm.last_unload_reason, "gaming_mode")
        self.assertGreaterEqual(warm.unload_calls, 2)


if __name__ == "__main__":
    unittest.main()
