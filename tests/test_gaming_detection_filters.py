from __future__ import annotations

import unittest
from unittest.mock import patch

from assistant.gaming_awareness import GamingAwarenessManager
from assistant.gaming_detection_filters import install_gaming_detection_filters


class FakeProcess:
    def __init__(self, pid: int, name: str, exe: str, cmdline=None):
        self.info = {"pid": pid, "name": name, "exe": exe, "cmdline": list(cmdline or [])}


class GamingDetectionFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_gaming_detection_filters()

    def _manager(self, **overrides):
        gaming = {
            "game_processes": [],
            "game_path_markers": ["/steamapps/common/"],
            "minecraft_command_markers": ["minecraft", "forge", "fabric-loader"],
            "ignored_game_processes": ["wallpaper32.exe", "wallpaper64.exe", "wallpaper_engine.exe"],
            "ignored_game_path_markers": ["/steamapps/common/wallpaper_engine/"],
        }
        gaming.update(overrides)
        return GamingAwarenessManager({"gaming_awareness": gaming})

    @patch("assistant.gaming_detection_filters.psutil.process_iter")
    def test_wallpaper_engine_is_ignored(self, process_iter):
        process_iter.return_value = [
            FakeProcess(101, "wallpaper64.exe", "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe")
        ]
        self.assertIsNone(self._manager()._scan_processes())

    @patch("assistant.gaming_detection_filters.psutil.process_iter")
    def test_ignored_utility_does_not_hide_real_game(self, process_iter):
        process_iter.return_value = [
            FakeProcess(101, "wallpaper64.exe", "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe"),
            FakeProcess(202, "ExampleGame.exe", "D:/SteamLibrary/steamapps/common/Example Game/ExampleGame.exe"),
        ]
        report = self._manager()._scan_processes()
        self.assertIsNotNone(report)
        self.assertEqual(report["pid"], 202)
        self.assertEqual(report["source"], "game_path")

    @patch("assistant.gaming_detection_filters.psutil.process_iter")
    def test_explicit_game_process_overrides_ignore(self, process_iter):
        process_iter.return_value = [
            FakeProcess(303, "wallpaper64.exe", "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe")
        ]
        report = self._manager(game_processes=["wallpaper64.exe"])._scan_processes()
        self.assertIsNotNone(report)
        self.assertEqual(report["pid"], 303)
        self.assertEqual(report["source"], "process")


if __name__ == "__main__":
    unittest.main()
