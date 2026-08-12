from __future__ import annotations

"""Bootstrap consolidado de Nova 0.6.x.

Desde v0.6.7 Memory/Workspace/Semantic Memory/Continuity son parte nativa de
``assistant.memory``. Agent/Tools/UI/TaskEngine todavía usan la base histórica
local v0.5, por lo que aquí se instalan únicamente adaptadores de compatibilidad
por dominio, no una cadena de runtimes versionados.
"""

_INSTALLED = False


def install_core_runtime():
    global _INSTALLED
    if _INSTALLED:
        return

    # MemoryStore ya incluye nativamente workspace, semantic memory y continuity.

    # Herramientas por dominio.
    from .tools_workspace import install_tools_v060
    from .tools_workspace_index import install_tools_v061
    from .tools_semantic import install_tools_v063
    from .tools_continuity import install_tools_v065
    from .tools_diagnostics import install_tools_v066
    install_tools_v060()
    install_tools_v061()
    install_tools_v063()
    install_tools_v065()
    install_tools_v066()

    # Doctor enriquecido y agente.
    from .doctor_semantic import install_doctor_v063
    from .doctor_diagnostics import install_doctor_v066
    install_doctor_v063()
    install_doctor_v066()

    from .agent_workspace import install_agent_v060
    from .agent_semantic import install_agent_v063
    from .agent_continuity import install_agent_v065
    from .agent_diagnostics import install_agent_v066
    install_agent_v060()
    install_agent_v063()
    install_agent_v065()
    install_agent_v066()

    # Interfaz y profiler al final para medir el comportamiento final.
    from .ui_workspace import install_ui_v060
    from .ui_semantic import install_ui_v063
    from .ui_continuity import install_ui_v065
    from .ui_diagnostics import install_ui_v066
    from .profiler_hooks import install_profiler_v066
    install_ui_v060()
    install_ui_v063()
    install_ui_v065()
    install_ui_v066()
    install_profiler_v066()

    _INSTALLED = True


def architecture_status() -> dict:
    """Describe el contrato del core sin ejecutar el LLM."""
    import importlib.util

    required_local = {
        "agent": "assistant.agent",
        "tools": "assistant.tools",
        "ui": "assistant.ui",
        "task_engine": "assistant.task_engine",
    }
    local = {name: importlib.util.find_spec(module) is not None for name, module in required_local.items()}
    return {
        "ok": all(local.values()),
        "bootstrap": "assistant.core_runtime",
        "legacy_local_contract": local,
        "github_managed_native": [
            "memory", "workspace", "workspace_index", "semantic_memory",
            "continuity", "doctor", "profiler", "self_repair",
        ],
        "compatibility_adapters": ["agent", "tools", "ui", "task_engine"],
        "versioned_runtime_chain": False,
    }
