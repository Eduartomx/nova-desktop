from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.v060_memory import install_memory_v060
from assistant.v063_memory import install_memory_v063

install_memory_v060()
install_memory_v063()

from assistant.memory import MemoryStore


class SemanticMemoryTests(unittest.TestCase):
    def make_store(self, root: Path) -> MemoryStore:
        store = MemoryStore(root / "assistant.db")
        store.configure_semantic_memory(
            {
                "enabled": True,
                "model": "test-embedding",
                "lazy_index": False,
                "minimum_semantic_score": 0.01,
            },
            "http://127.0.0.1:11434",
        )
        return store

    def test_lexical_fallback_when_model_missing(self):
        with tempfile.TemporaryDirectory() as td:
            store = self.make_store(Path(td))
            store.set_memory("loader", "Forge 1.20.1", category="decision")
            store.semantic_memory.model_available = lambda refresh=False: (False, "modelo ausente")
            rows = store.search_memory("forge", limit=5)
            self.assertTrue(any(x["key"] == "loader" for x in rows))
            self.assertEqual(rows[0].get("retrieval"), "lexical")

    def test_hybrid_search_finds_semantic_match_without_shared_words(self):
        with tempfile.TemporaryDirectory() as td:
            store = self.make_store(Path(td))
            store.set_memory("causa_crash", "Biome Makeover chocaba con Accessories", category="solution", importance=0.9)
            store.set_memory("idioma", "Responder en español", category="preference", importance=0.5)
            store.semantic_memory.model_available = lambda refresh=False: (True, "ok")

            def fake_embed(texts):
                vectors = []
                for text in texts:
                    lowered = text.casefold()
                    if "biome makeover" in lowered or "fallo parecido" in lowered:
                        vectors.append([1.0, 0.0, 0.0])
                    else:
                        vectors.append([0.0, 1.0, 0.0])
                return vectors

            store.semantic_memory.embed = fake_embed
            indexed = store.semantic_reindex(force=True)
            self.assertTrue(indexed["ok"])
            self.assertEqual(indexed["indexed"], 2)

            rows = store.search_memory("¿habíamos visto un fallo parecido?", limit=3)
            self.assertTrue(rows)
            self.assertEqual(rows[0]["key"], "causa_crash")
            self.assertEqual(rows[0]["retrieval"], "hybrid")
            self.assertGreater(rows[0]["semantic_score"], 0.9)

    def test_memory_update_invalidates_old_embedding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = self.make_store(root)
            store.set_memory("servidor", "Forge", category="fact")
            store.semantic_memory.model_available = lambda refresh=False: (True, "ok")
            store.semantic_memory.embed = lambda texts: [[1.0, 0.0] for _ in texts]
            self.assertTrue(store.semantic_reindex(force=True)["ok"])

            with store._lock, store._connection() as conn:
                before = conn.execute("SELECT COUNT(*) AS n FROM memory_embeddings").fetchone()["n"]
            self.assertEqual(before, 1)

            store.set_memory("servidor", "Fabric", category="fact")
            with store._lock, store._connection() as conn:
                after = conn.execute("SELECT COUNT(*) AS n FROM memory_embeddings").fetchone()["n"]
            self.assertEqual(after, 0)

            moved = root / "assistant_moved.db"
            store.db_path.rename(moved)
            self.assertTrue(moved.exists())


if __name__ == "__main__":
    unittest.main()
