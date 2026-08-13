from __future__ import annotations

import time
import unittest

from assistant.gaming_awareness import GamingAwarenessManager
from assistant.gaming_detection_filters import install_gaming_detection_filters
from assistant.gaming_reliability import install_gaming_reliability
from assistant.ui_gaming_events import apply_gaming_report


class FakeWarm:
    def __init__(self):
        self.loaded = True
        self.preload_on_start = True
        self.suppressed = ""
        self.override = None
        self.last_unload_reason = ""
        self.unload_calls = 0
        self.preload_calls = 0

    def cached_status(self):
        return {
            "loaded": self.loaded,
            "warming": False,
            "active_inferences": 0,
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


class FakePerception:
    def __init__(self, state=None):
        self.state = state or {"sampled_at": time.time(), "foreground": {}, "external": {}}
        self.runtime_poll = None

    def current(self, refresh=False):
        return self.state

    def set_runtime_poll_interval_ms(self, value):
        self.runtime_poll = value

    def effective_poll_interval_ms(self):
        return int(self.runtime_poll or 1100)


class FakeVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = str(value)


class FakeUI:
    def __init__(self, warm):
        self.gaming_mode_var = FakeVar()
        self.llm_warm_var = FakeVar()
        self.llm_warm_manager = warm


class GamingReliabilityTests(unittest.TestCase):
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
            "restore_preload_after_game": True,
            "restore_delay_seconds": 0.08,
            "perception_max_age_seconds": 1.0,
            "game_processes": ["game.exe", "fortniteclient-win64-shipping.exe", "robloxplayerbeta.exe", "vrchat.exe"],
            "game_path_markers": ["/steamapps/common/", "\\epic games\\"],
            "ignored_game_processes": [
                "wallpaper32.exe", "wallpaper64.exe", "steam.exe", "steamwebhelper.exe",
                "minecraftlauncher.exe", "crashpad_handler.exe",
            ],
            "ignored_game_path_markers": ["/steamapps/common/wallpaper_engine/"],
            "minecraft_command_markers": ["minecraft", "forge", "fabric-loader"],
        }
        gaming.update(overrides)
        return {"model": "qwen3.5:4b", "gaming_awareness": gaming}

    @staticmethod
    def _identity_table(table):
        def sensor(pid):
            row = table.get(int(pid))
            return dict(row) if row else None
        return sensor

    def test_real_game_enters_and_dead_pid_exits(self):
        warm = FakeWarm()
        live = {10: {"alive": True, "process": "game.exe", "create_time": 100.0}}
        current = {"pid": 10, "process": "game.exe", "source": "process", "reason": "configured", "foreground": False}
        manager = GamingAwarenessManager(
            self._config(), warm_manager=warm, perception=FakePerception(),
            process_sensor=lambda: dict(current) if current else None,
            identity_sensor=self._identity_table(live), gpu_sensor=lambda: {},
        )
        report = manager.tick()
        self.assertTrue(report["active"])
        self.assertEqual(report["game"]["pid"], 10)
        self.assertEqual(report["game"]["source"], "process")

        current.clear()
        live.clear()
        report = manager.tick()
        self.assertFalse(report["active"])
        self.assertEqual(report["game"], {})

    def test_wallpaper_engine_never_enters(self):
        manager = GamingAwarenessManager(
            self._config(), warm_manager=FakeWarm(), perception=FakePerception(),
            process_sensor=lambda: {
                "pid": 20, "process": "wallpaper64.exe",
                "exe": "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe",
                "source": "process",
            },
            identity_sensor=self._identity_table({20: {"alive": True, "process": "wallpaper64.exe", "create_time": 1}}),
            gpu_sensor=lambda: {},
        )
        self.assertFalse(manager.tick()["active"])

    def test_launcher_does_not_keep_mode_after_game_dies(self):
        warm = FakeWarm()
        live = {
            30: {"alive": True, "process": "game.exe", "create_time": 1},
            31: {"alive": True, "process": "steam.exe", "create_time": 2},
        }
        state = {"kind": "game"}

        def process_sensor():
            if state["kind"] == "game":
                return {"pid": 30, "process": "game.exe", "source": "process"}
            return {"pid": 31, "process": "steam.exe", "source": "process"}

        manager = GamingAwarenessManager(
            self._config(), warm_manager=warm, perception=FakePerception(),
            process_sensor=process_sensor, identity_sensor=self._identity_table(live), gpu_sensor=lambda: {},
        )
        self.assertTrue(manager.tick()["active"])
        state["kind"] = "launcher"
        live.pop(30)
        self.assertFalse(manager.tick()["active"])

    def test_stale_perception_is_discarded(self):
        stale = {
            "sampled_at": time.time() - 30,
            "foreground": {
                "pid": 40, "process": "game.exe", "app_kind": "game",
                "exe": "D:/Steam/steamapps/common/Game/game.exe", "is_nova": False,
            },
            "external": {},
        }
        manager = GamingAwarenessManager(
            self._config(), warm_manager=FakeWarm(), perception=FakePerception(stale),
            process_sensor=lambda: None,
            identity_sensor=self._identity_table({40: {"alive": True, "process": "game.exe", "create_time": 1}}),
            gpu_sensor=lambda: {},
        )
        self.assertFalse(manager.tick()["active"])

    def test_dead_perception_pid_is_discarded(self):
        state = {
            "sampled_at": time.time(),
            "foreground": {"pid": 41, "process": "game.exe", "app_kind": "game", "is_nova": False},
            "external": {},
        }
        manager = GamingAwarenessManager(
            self._config(), warm_manager=FakeWarm(), perception=FakePerception(state),
            process_sensor=lambda: None, identity_sensor=self._identity_table({}), gpu_sensor=lambda: {},
        )
        self.assertFalse(manager.tick()["active"])

    def test_foreground_library_path_requires_foreground_and_records_source(self):
        state = {
            "sampled_at": time.time(),
            "foreground": {
                "pid": 50, "process": "UnknownGame.exe", "app_kind": "other",
                "exe": "D:/SteamLibrary/steamapps/common/Unknown Game/UnknownGame.exe", "is_nova": False,
            },
            "external": {},
        }
        manager = GamingAwarenessManager(
            self._config(game_processes=[]), warm_manager=FakeWarm(), perception=FakePerception(state),
            process_sensor=lambda: None,
            identity_sensor=self._identity_table({50: {"alive": True, "process": "UnknownGame.exe", "create_time": 8}}),
            gpu_sensor=lambda: {},
        )
        report = manager.tick()
        self.assertTrue(report["active"])
        self.assertEqual(report["game"]["source"], "foreground_game_path")

    def test_ui_receives_enter_and_exit_events(self):
        warm = FakeWarm()
        live = {60: {"alive": True, "process": "game.exe", "create_time": 3}}
        current = {"pid": 60, "process": "game.exe", "source": "process", "reason": "configured"}
        manager = GamingAwarenessManager(
            self._config(), warm_manager=warm, perception=FakePerception(),
            process_sensor=lambda: dict(current) if current else None,
            identity_sensor=self._identity_table(live), gpu_sensor=lambda: {},
        )
        ui = FakeUI(warm)
        events = []

        def listener(event, report):
            events.append(event)
            apply_gaming_report(ui, report)

        manager.add_state_listener(listener)
        manager.tick()
        self.assertIn("entered", events)
        self.assertIn("game.exe", ui.gaming_mode_var.value)
        self.assertIn("process", ui.gaming_mode_var.value)

        current.clear()
        live.clear()
        manager.tick()
        self.assertIn("exited", events)
        self.assertEqual(ui.gaming_mode_var.value, "🎮 Juego: auto")

    def test_reentry_invalidates_pending_qwen_restore(self):
        warm = FakeWarm()
        live = {70: {"alive": True, "process": "game.exe", "create_time": 1}}
        current = {"pid": 70, "process": "game.exe", "source": "process"}
        manager = GamingAwarenessManager(
            self._config(restore_delay_seconds=0.12), warm_manager=warm, perception=FakePerception(),
            process_sensor=lambda: dict(current) if current else None,
            identity_sensor=self._identity_table(live), gpu_sensor=lambda: {},
        )
        manager.tick()
        self.assertEqual(warm.unload_calls, 1)

        current.clear()
        live.clear()
        manager.tick()
        scheduled_generation = manager.status()["restore_generation"]

        live[71] = {"alive": True, "process": "game.exe", "create_time": 2}
        current.update({"pid": 71, "process": "game.exe", "source": "process"})
        manager.tick()
        self.assertGreater(manager.status()["restore_generation"], scheduled_generation)
        time.sleep(0.18)
        self.assertEqual(warm.preload_calls, 0)
        self.assertTrue(manager.status()["active"])

    def test_manual_modes_remain_functional(self):
        manager = GamingAwarenessManager(
            self._config(), warm_manager=FakeWarm(), perception=FakePerception(),
            process_sensor=lambda: None, identity_sensor=self._identity_table({}), gpu_sensor=lambda: {},
        )
        self.assertTrue(manager.set_mode("on")["active"])
        self.assertEqual(manager.status()["game"]["source"], "manual")
        self.assertFalse(manager.set_mode("off")["active"])
        self.assertEqual(manager.set_mode("auto")["mode"], "auto")


if __name__ == "__main__":
    unittest.main()
