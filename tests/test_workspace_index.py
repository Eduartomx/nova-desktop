from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from assistant.v060_memory import install_memory_v060

install_memory_v060()
from assistant.memory import MemoryStore
from assistant.workspace import WorkspaceManager
from assistant.workspace_index import WorkspaceIndexer


class WorkspaceIndexTests(unittest.TestCase):
    def test_incremental_index_changes_and_search(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "src").mkdir()
            (project / "src" / "main.py").write_text("print('v1')\n", encoding="utf-8")
            (project / "server.properties").write_text("server-port=25565\n", encoding="utf-8")
            ignored = project / "node_modules" / "pkg"
            ignored.mkdir(parents=True)
            (ignored / "huge.js").write_text("ignored", encoding="utf-8")

            store = MemoryStore(root / "data" / "assistant.db")
            ws = WorkspaceManager(store).create(str(project), name="Index Test")
            indexer = WorkspaceIndexer(store, {"index_max_files": 500, "index_max_depth": 5})

            first = indexer.index(ws)
            self.assertTrue(first["ok"])
            self.assertEqual(first["added"], 2)
            self.assertEqual(first["modified"], 0)
            self.assertEqual(first["removed"], 0)
            self.assertFalse(any("node_modules" in r["rel_path"] for r in indexer.search(ws["id"], "huge", 20)))
            self.assertTrue(any(r["rel_path"] == "src/main.py" for r in indexer.search(ws["id"], "main", 20)))

            time.sleep(0.01)
            (project / "src" / "main.py").write_text("print('v2 changed')\n", encoding="utf-8")
            (project / "new_config.json").write_text("{}", encoding="utf-8")
            (project / "server.properties").unlink()

            second = indexer.index(ws)
            self.assertEqual(second["added"], 1)
            self.assertEqual(second["modified"], 1)
            self.assertEqual(second["removed"], 1)
            changes = indexer.changes(ws["id"], run_id=second["run_id"], limit=20)
            by_path = {x["rel_path"]: x["change_type"] for x in changes}
            self.assertEqual(by_path["new_config.json"], "added")
            self.assertEqual(by_path["src/main.py"], "modified")
            self.assertEqual(by_path["server.properties"], "removed")

            status = indexer.status(ws["id"])
            self.assertEqual(status["indexed_files"], 2)
            self.assertEqual(status["last_run"]["id"], second["run_id"])

            moved = store.db_path.with_name("index_moved.db")
            store.db_path.rename(moved)
            self.assertTrue(moved.exists())


if __name__ == "__main__":
    unittest.main()
