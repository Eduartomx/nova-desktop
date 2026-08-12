from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from assistant.agent_perception import perception_direct_intent
from assistant.perception import PerceptionEngine, classify_app


class FakeMemory:
    def __init__(self, workspace):
        self.workspace = workspace

    def list_workspaces(self, limit=100):
        return [dict(self.workspace)]

    def active_workspace(self):
        return dict(self.workspace)


class SequenceSensor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.index = 0

    def __call__(self):
        row = self.rows[min(self.index, len(self.rows) - 1)]
        self.index += 1
        return dict(row)


class PerceptionEngineTests(unittest.TestCase):
    def test_workspace_detection_and_last_external_survives_nova_focus(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "Servidor Nova"
            project.mkdir()
            workspace = {
                "id": 7,
                "name": "Servidor Nova",
                "path": str(project),
                "kind": "minecraft_server",
                "is_active": True,
            }
            sensor = SequenceSensor([
                {
                    "ok": True,
                    "pid": 4242,
                    "process": "Code.exe",
                    "title": "server.properties - Servidor Nova - Visual Studio Code",
                    "exe": r"C:\Program Files\Microsoft VS Code\Code.exe",
                    "cwd": str(project / "config"),
                },
                {
                    "ok": True,
                    "pid": os.getpid(),
                    "process": "python.exe",
                    "title": "Nova · Asistente local v0.7.0",
                    "exe": "python.exe",
                    "cwd": str(root),
                },
            ])
            engine = PerceptionEngine(
                {"enabled": True, "persist_events": True, "persist_window_titles": False},
                memory=FakeMemory(workspace),
                db_path=root / "perception.db",
                sensor=sensor,
                system_sensor=lambda: {"cpu_percent": 12, "memory_percent": 44, "nova_memory_mb": 80},
            )

            first = engine.sample_once()
            self.assertEqual(first["external"]["process"], "Code.exe")
            self.assertEqual(first["external"]["app_kind"], "code_editor")
            self.assertEqual(first["probable_workspace"]["id"], 7)
            self.assertGreaterEqual(first["probable_workspace"]["confidence"], 0.98)

            second = engine.sample_once()
            self.assertTrue(second["foreground"]["is_nova"])
            self.assertEqual(second["external"]["process"], "Code.exe")
            self.assertEqual(second["probable_workspace"]["id"], 7)

            events = engine.recent_events(20)
            self.assertTrue(events)
            self.assertTrue(any(x["event_type"] == "workspace_candidate" for x in events))
            self.assertTrue(all("title" not in (x.get("metadata") or {}) for x in events))

            moved = root / "perception_moved.db"
            engine.db_path.rename(moved)
            self.assertTrue(moved.exists())

    def test_privacy_status_and_context_format(self):
        with tempfile.TemporaryDirectory() as td:
            engine = PerceptionEngine(
                {"enabled": True, "persist_events": False},
                db_path=Path(td) / "perception.db",
                sensor=lambda: {
                    "ok": True,
                    "pid": 9001,
                    "process": "msedge.exe",
                    "title": "GitHub - Microsoft Edge",
                    "exe": "",
                    "cwd": "",
                },
                system_sensor=lambda: {"cpu_percent": 4, "memory_percent": 30, "nova_memory_mb": 70},
            )
            status = engine.status(refresh=True)
            self.assertTrue(status["enabled"])
            self.assertFalse(status["captures_screen"])
            self.assertFalse(status["captures_keyboard"])
            self.assertFalse(status["reads_clipboard"])
            self.assertEqual(status["app_kind"], "browser")
            text = engine.compact_context()
            self.assertIn("msedge.exe", text)
            self.assertIn("dato no confiable", text)

    def test_app_classification_and_direct_routing(self):
        self.assertEqual(classify_app("Code.exe", "Nova"), "code_editor")
        self.assertEqual(classify_app("javaw.exe", "Minecraft 1.20.1"), "game")
        self.assertEqual(classify_app("javaw.exe", "Some Java Tool"), "java_app")
        self.assertEqual(perception_direct_intent("¿Qué aplicación tengo abierta?"), "current")
        self.assertEqual(perception_direct_intent("¿Está funcionando tu percepción?"), "status")
        self.assertEqual(perception_direct_intent("¿Qué cambios de contexto viste?"), "recent")
        self.assertIsNone(perception_direct_intent("abre Minecraft"))


if __name__ == "__main__":
    unittest.main()
