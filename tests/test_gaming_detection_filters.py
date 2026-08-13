from __future__ import annotations

import unittest
from unittest.mock import patch

from assistant.gaming_awareness import GamingAwarenessManager
from assistant.gaming_detection_filters import install_gaming_detection_filters


class FakeProcess:
    def __init__(self, pid: int, name: str, exe: str, cmdline=None, create_time=1.0):
        self.info = {
            "pid": pid,
            "name": name,
            "exe": exe,
            "cmdline": list(cmdline or []),
            "create_time": create_time,
        }


class GamingDetectionFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_gaming_detection_filters()

    def _manager(self, **overrides):
        gaming = {
            "game_processes": [],
            "game_path_markers": ["/steamapps/common/"],
            "minecraft_command_markers": ["minecraft", "forge", "fabric-loader"],
            "ignored_game_processes": [
                "wallpaper32.exe", "wallpaper64.exe", "wallpaper_engine.exe",
                "steam.exe", "steamwebhelper.exe", "minecraftlauncher.exe",
            ],
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
    def test_library_path_alone_is_not_a_game_signal(self, process_iter):
        process_iter.return_value = [
            FakeProcess(202, "ExampleUtility.exe", "D:/SteamLibrary/steamapps/common/Example Tool/ExampleUtility.exe")
        ]
        self.assertIsNone(self._manager()._scan_processes())

    @patch("assistant.gaming_detection_filters.psutil.process_iter")
    def test_launcher_and_helper_are_ignored(self, process_iter):
        process_iter.return_value = [
            FakeProcess(301, "steamwebhelper.exe", "C:/Program Files/Steam/bin/steamwebhelper.exe"),
            FakeProcess(302, "MinecraftLauncher.exe", "C:/XboxGames/Minecraft Launcher/MinecraftLauncher.exe"),
        ]
        self.assertIsNone(self._manager()._scan_processes())

    @patch("assistant.gaming_detection_filters.psutil.process_iter")
    def test_explicit_game_process_overrides_ignore(self, process_iter):
        process_iter.return_value = [
            FakeProcess(303, "wallpaper64.exe", "C:/Steam/steamapps/common/wallpaper_engine/wallpaper64.exe")
        ]
        report = self._manager(game_processes=["wallpaper64.exe"])._scan_processes()
        self.assertIsNotNone(report)
        self.assertEqual(report["pid"], 303)
        self.assertEqual(report["source"], "process")

    @patch("assistant.gaming_detection_filters.psutil.process_iter")
    def test_minecraft_java_detection_is_preserved(self, process_iter):
        process_iter.return_value = [
            FakeProcess(404, "javaw.exe", "C:/Java/bin/javaw.exe", ["javaw", "net.minecraft.client.main.Main", "--fabric-loader"])
        ]
        report = self._manager()._scan_processes()
        self.assertIsNotNone(report)
        self.assertEqual(report["pid"], 404)
        self.assertEqual(report["source"], "minecraft_java")


if __name__ == "__main__":
    unittest.main()
