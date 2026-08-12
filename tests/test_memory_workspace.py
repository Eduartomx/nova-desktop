from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.memory import MemoryStore
from assistant.workspace import WorkspaceManager, detect_workspace_kind, workspace_snapshot


class MemoryWorkspaceTests(unittest.TestCase):
    def test_workspace_memory_and_task_continuity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "data" / "assistant.db"
            project = root / "server"
            (project / "mods").mkdir(parents=True)
            (project / "mods" / "example.jar").write_bytes(b"jar")
            (project / "server.properties").write_text(
                "motd=Nova Test\nserver-port=25565\nmax-players=8\n",
                encoding="utf-8",
            )

            store = MemoryStore(db)
            manager = WorkspaceManager(store)
            ws = manager.create(str(project), name="Servidor Test")
            self.assertTrue(ws["is_active"])
            self.assertEqual(ws["kind"], "minecraft_server")
            self.assertEqual(store.active_workspace()["name"], "Servidor Test")

            store.set_memory("loader", "Forge 1.20.1", category="decision", workspace_id=ws["id"])
            store.set_memory("idioma", "español", category="preference")
            store.set_memory("idioma", "español de Chile", category="preference")
            results = store.search_memory("forge loader", workspace_id=ws["id"], limit=5)
            self.assertTrue(any(x["key"] == "loader" for x in results))
            global_items = [x for x in store.recent_memory_items(20) if x["key"] == "idioma"]
            self.assertEqual(len(global_items), 1)
            self.assertEqual(global_items[0]["value"], "español de Chile")

            task_id = store.create_task("Revisar latest.log", {"steps": []}, status="running")
            task = store.get_task(task_id)
            self.assertEqual(task["workspace_id"], ws["id"])
            self.assertEqual(task["workspace_name"], "Servidor Test")

            snap = workspace_snapshot(project)
            self.assertEqual(snap["kind"], "minecraft_server")
            self.assertEqual(snap["mods_count"], 1)
            self.assertEqual(snap["server_properties"]["server-port"], "25565")

            moved = db.with_name("assistant_moved.db")
            db.rename(moved)
            self.assertTrue(moved.exists())

    def test_project_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text("{}", encoding="utf-8")
            kind, markers = detect_workspace_kind(root)
            self.assertEqual(kind, "node")
            self.assertIn("package.json", markers)


if __name__ == "__main__":
    unittest.main()
