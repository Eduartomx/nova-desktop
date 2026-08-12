from __future__ import annotations

import functools
import time
from typing import Any

from .profiler import get_profiler


def _wrap_method(cls, name: str, operation: str, config_getter=None):
    original = getattr(cls, name, None)
    if not callable(original) or getattr(original, "_nova_profiled", False):
        return

    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        cfg = None
        try:
            cfg = config_getter(self) if config_getter else getattr(self, "config", None)
        except Exception:
            cfg = None
        profiler = get_profiler(cfg if isinstance(cfg, dict) else None)
        started = time.perf_counter()
        ok = False
        try:
            result = original(self, *args, **kwargs)
            ok = True
            return result
        finally:
            profiler.record(operation, (time.perf_counter() - started) * 1000.0, ok)

    wrapped._nova_profiled = True
    setattr(cls, name, wrapped)


def install_profiler_v066():
    """Instrumenta puntos estables sin acoplar el profiler al flujo principal."""
    # Agente completo y posibles llamadas internas de modelo.
    try:
        from . import agent as agent_mod
        Agent = agent_mod.LocalAgent
        _wrap_method(Agent, "ask", "agent.total")
        for name in ("_chat", "_ollama_chat", "_call_model", "_run_model", "_model_call"):
            _wrap_method(Agent, name, "llm." + name.lstrip("_"))
    except Exception:
        pass

    # Búsqueda de memoria final (incluye la capa semántica si está activa).
    try:
        from . import memory as memory_mod
        _wrap_method(memory_mod.MemoryStore, "search_memory", "memory.search", lambda _self: None)
    except Exception:
        pass

    try:
        from .semantic_memory import SemanticMemoryEngine
        _wrap_method(SemanticMemoryEngine, "embed", "memory.embed", lambda _self: None)
        _wrap_method(SemanticMemoryEngine, "search", "memory.semantic_search", lambda _self: None)
    except Exception:
        pass

    # Cada herramienta expuesta se mide por nombre; esto cubre Browser Agent,
    # sistema, workspaces, memoria y herramientas futuras sin hardcodearlas.
    try:
        from . import tools as tools_mod
        LocalTools = tools_mod.LocalTools
        names = {
            str(x.get("function", {}).get("name") or "")
            for x in getattr(tools_mod, "TOOL_SCHEMAS", [])
        }
        for name in sorted(x for x in names if x and hasattr(LocalTools, x)):
            _wrap_method(LocalTools, name, f"tool.{name}")
    except Exception:
        pass

    # Task Engine: nombres históricos soportados de forma condicional.
    try:
        from . import task_engine as task_mod
        for cls_name in ("TaskEngine", "AutonomyEngine"):
            cls = getattr(task_mod, cls_name, None)
            if cls is None:
                continue
            for method in ("run", "run_task", "execute", "execute_task"):
                _wrap_method(cls, method, f"task_engine.{method}")
    except Exception:
        pass

    # Doctor también se perfila, útil para saber si una comprobación se degrada.
    try:
        from .doctor import NovaDoctor
        _wrap_method(NovaDoctor, "run", "doctor.run")
    except Exception:
        pass

    return True
