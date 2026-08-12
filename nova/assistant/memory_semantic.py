from __future__ import annotations

from .semantic_memory import SemanticMemoryEngine


def install_memory_v063():
    from . import memory as memory_mod

    MemoryStore = memory_mod.MemoryStore
    if getattr(MemoryStore, "_nova_v063_patched", False):
        return MemoryStore

    original_init = MemoryStore.__init__
    original_set_memory = MemoryStore.set_memory
    lexical_search = MemoryStore.search_memory

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.semantic_memory = SemanticMemoryEngine(self, config={"enabled": False})

    def configure_semantic_memory(self, config=None, ollama_host=None):
        engine = getattr(self, "semantic_memory", None)
        if engine is None:
            engine = SemanticMemoryEngine(self, config=config or {}, ollama_host=ollama_host or "http://127.0.0.1:11434")
            self.semantic_memory = engine
        else:
            engine.configure(config or {}, ollama_host)
        return engine

    def set_memory(self, key, value, category="fact", workspace_id=None, importance=0.5, source="user"):
        result = original_set_memory(
            self,
            key,
            value,
            category=category,
            workspace_id=workspace_id,
            importance=importance,
            source=source,
        )
        try:
            self.semantic_memory.invalidate_by_key(key, workspace_id)
        except Exception:
            pass
        return result

    def search_memory_lexical(self, query, limit=8, workspace_id=None):
        return lexical_search(self, query, limit, workspace_id)

    def search_memory(self, query, limit=8, workspace_id=None):
        engine = getattr(self, "semantic_memory", None)
        if engine is None:
            return lexical_search(self, query, limit, workspace_id)
        return engine.search(
            query,
            limit,
            workspace_id,
            lambda q, lim, wid: lexical_search(self, q, lim, wid),
        )

    def semantic_status(self, workspace_id=None, refresh=False):
        engine = getattr(self, "semantic_memory", None)
        if engine is None:
            return {"enabled": False, "model_available": False, "detail": "Semantic Memory no inicializada"}
        return engine.status(workspace_id=workspace_id, refresh=bool(refresh))

    def semantic_reindex(self, workspace_id=None, force=False, limit=1000):
        engine = getattr(self, "semantic_memory", None)
        if engine is None:
            return {"ok": False, "detail": "Semantic Memory no inicializada"}
        return engine.reindex(workspace_id=workspace_id, force=bool(force), limit=limit)

    MemoryStore.__init__ = init
    MemoryStore.configure_semantic_memory = configure_semantic_memory
    MemoryStore.set_memory = set_memory
    MemoryStore.search_memory_lexical = search_memory_lexical
    MemoryStore.search_memory = search_memory
    MemoryStore.semantic_status = semantic_status
    MemoryStore.semantic_reindex = semantic_reindex
    MemoryStore._nova_v063_patched = True
    return MemoryStore
