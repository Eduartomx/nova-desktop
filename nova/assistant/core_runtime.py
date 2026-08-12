from __future__ import annotations

"""Bootstrap consolidado de Nova 0.6.x.

La base histórica de Agent/Tools/UI/TaskEngine aún procede de la instalación local
v0.5. Esta capa concentra todas las extensiones administradas por GitHub en un
único punto de entrada y elimina la cadena de runtimes versionados.
"""

_INSTALLED = False


def install_core_runtime():
    global _INSTALLED
    if _INSTALLED:
        return

    # Memoria y workspaces primero: otras capas dependen de estos métodos.
    from .memory_workspace import install_memory_v060
    from .memory_semantic import install_memory_v063
    from .memory_continuity import install_memory_v065
    install_memory_v060()
    install_memory_v063()
    install_memory_v065()

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

    # Doctor antes de UI para que la ventana ya reciba el reporte enriquecido.
    from .doctor_semantic import install_doctor_v063
    from .doctor_diagnostics import install_doctor_v066
    install_doctor_v063()
    install_doctor_v066()

    # Agente: workspace -> semantic -> continuity -> diagnostics.
    from .agent_workspace import install_agent_v060
    from .agent_semantic import install_agent_v063
    from .agent_continuity import install_agent_v065
    from .agent_diagnostics import install_agent_v066
    install_agent_v060()
    install_agent_v063()
    install_agent_v065()
    install_agent_v066()

    # Interfaz y profiler al final para observar el comportamiento ya consolidado.
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
    """Describe el contrato del core sin importar/ejecutar el LLM."""
    import importlib.util

    required_local = {
        "agent": "assistant.agent",
        "tools": "assistant.tools",
        "ui": "assistant.ui",
        "task_engine": "assistant.task_engine",
    }
    local = {name: importlib.util.find_spec(module) is not None for name, module in required_local.items()}
    managed = {
        "memory": True,
        "workspace": True,
        "semantic_memory": True,
        "continuity": True,
        "doctor": True,
        "profiler": True,
        "self_repair": True,
    }
    return {
        "ok": all(local.values()),
        "bootstrap": "assistant.core_runtime",
        "legacy_local_contract": local,
        "github_managed_domains": managed,
        "versioned_runtime_chain": False,
    }
