from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.memory import MemoryStore
from assistant.tools import LocalTools
from assistant.tools_file_safety import install_tools_file_safety


install_tools_file_safety()


class FileWriteSafetyTests(unittest.TestCase):
    def test_overwrite_creates_backup_with_previous_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "config.txt"
            target.write_text("version-anterior", encoding="utf-8")
            memory = MemoryStore(root / "memory.db")
            tools = LocalTools(
                {
                    "security": {
                        "profile": "trusted",
                        "restrict_files_to_allowed_roots": True,
                        "allowed_roots": [str(root)],
                        "backup_overwritten_files": True,
                    }
                },
                memory,
            )
            result = tools.write_file(str(target), "version-nueva")
            self.assertTrue(result["ok"], result)
            self.assertEqual(target.read_text(encoding="utf-8"), "version-nueva")
            backup = Path(result.get("backup") or "")
            self.assertTrue(backup.is_file(), result)
            self.assertEqual(backup.read_text(encoding="utf-8"), "version-anterior")

    def test_first_write_does_not_create_unnecessary_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "new.txt"
            memory = MemoryStore(root / "memory.db")
            tools = LocalTools(
                {
                    "security": {
                        "profile": "trusted",
                        "restrict_files_to_allowed_roots": True,
                        "allowed_roots": [str(root)],
                        "backup_overwritten_files": True,
                    }
                },
                memory,
            )
            result = tools.write_file(str(target), "nuevo")
            self.assertTrue(result["ok"], result)
            self.assertNotIn("backup", result)


if __name__ == "__main__":
    unittest.main()
