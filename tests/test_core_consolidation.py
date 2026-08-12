from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.core_runtime import architecture_status
from assistant.memory import MemoryStore


class CoreConsolidationTests(unittest.TestCase):
    def test_no_versioned_runtime_or_patch_files_remain(self):
        assistant_dir = Path(__file__).resolve().parents[1] / "nova" / "assistant"
        leftovers = sorted(p.name for p in assistant_dir.glob("v0*.py"))
        self.assertEqual(leftovers, [])

    def test_app_uses_single_core_bootstrap(self):
        app = (Path(__file__).resolve().parents[1] / "nova" / "app.py").read_text(encoding="utf-8")
        self.assertIn("from assistant.core_runtime import install_core_runtime", app)
        self.assertEqual(app.count("install_core_runtime()"), 1)
        self.assertNotIn("install_v060", app)
        self.assertNotIn("install_v061", app)
        self.assertNotIn("install_v063", app)
        self.assertNotIn("install_v065", app)
        self.assertNotIn("install_v066", app)

    def test_memory_features_are_native_without_installers(self):
        with tempfile.TemporaryDirectory() as td:
            store = MemoryStore(Path(td) / "assistant.db")
            for name in (
                "create_workspace", "active_workspace", "search_memory",
                "configure_semantic_memory", "semantic_reindex",
                "configure_continuity", "continuity_resume", "continuity_checkpoint",
            ):
                self.assertTrue(hasattr(store, name), name)
            self.assertFalse(hasattr(MemoryStore, "_nova_v060_patched"))
            self.assertFalse(hasattr(MemoryStore, "_nova_v063_patched"))
            self.assertFalse(hasattr(MemoryStore, "_nova_v065_patched"))

    def test_architecture_contract_has_no_versioned_runtime_chain(self):
        status = architecture_status()
        self.assertEqual(status["bootstrap"], "assistant.core_runtime")
        self.assertFalse(status["versioned_runtime_chain"])
        self.assertIn("memory", status["github_managed_native"])
        self.assertIn("perception", status["github_managed_native"])
        self.assertIn("agent", status["compatibility_adapters"])


if __name__ == "__main__":
    unittest.main()
