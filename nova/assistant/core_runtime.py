from __future__ import annotations

"""Bootstrap consolidado de Nova.

Desde v0.6.7 Memory/Workspace/Semantic Memory/Continuity y Nova Doctor son
módulos nativos administrados por GitHub. En v0.7 Perception Engine, Context
Intelligence, Workspace Auto-Detection, Anomaly Detection y Event-driven Vision
se suman como dominios nativos. En v0.8 Skills Engine añade playbooks locales
declarativos y reutilizables. Agent/Tools/UI/TaskEngine todavía usan la base
histórica local v0.5, por lo que aquí se instalan únicamente adaptadores por dominio.
"""

_INSTALLED = False


def install_core_runtime():
    global _INSTALLED
    if _INSTALLED:
        return

    # Herramientas por dominio.
    from .tools_workspace import install_tools_v060
    from .tools_workspace_index import install_tools_v061
    from .tools_semantic import install_tools_v063
    from .tools_continuity import install_tools_v065
    from .tools_diagnostics import install_tools_v066
    from .tools_perception import install_tools_perception
    from .tools_context_intelligence import install_tools_context_intelligence
    from .tools_workspace_autodetect import install_tools_workspace_autodetect
    from .tools_anomaly import install_tools_anomaly
    from .tools_vision import install_tools_vision
    from .tools_skills import install_tools_skills
    install_tools_v060()
    install_tools_v061()
    install_tools_v063()
    install_tools_v065()
    install_tools_v066()
    install_tools_perception()
    install_tools_context_intelligence()
    install_tools_workspace_autodetect()
    install_tools_anomaly()
    install_tools_vision()
    install_tools_skills()

    # Agent: contexto estructurado primero; visión como fallback y Skills al final
    # para poder reutilizar todo el conjunto de herramientas ya instalado.
    from .agent_workspace import install_agent_v060
    from .agent_semantic import install_agent_v063
    from .agent_continuity import install_agent_v065
    from .agent_diagnostics import install_agent_v066
    from .agent_perception import install_agent_perception
    from .agent_anomaly import install_agent_anomaly
    from .agent_vision import install_agent_vision
    from .agent_skills import install_agent_skills
    install_agent_v060()
    install_agent_v063()
    install_agent_v065()
    install_agent_v066()
    install_agent_perception()
    install_agent_anomaly()
    install_agent_vision()
    install_agent_skills()

    # UI y hooks del profiler al final. Skills agrega un gestor ligero sin
    # sustituir la interfaz histórica.
    from .ui_workspace import install_ui_v060
    from .ui_semantic import install_ui_v063
    from .ui_continuity import install_ui_v065
    from .ui_diagnostics import install_ui_v066
    from .ui_perception import install_ui_perception
    from .ui_workspace_autodetect import install_ui_workspace_autodetect
    from .ui_anomaly import install_ui_anomaly
    from .ui_vision import install_ui_vision
    from .ui_skills import install_ui_skills
    from .profiler_hooks import install_profiler_v066
    install_ui_v060()
    install_ui_v063()
    install_ui_v065()
    install_ui_v066()
    install_ui_perception()
    install_ui_workspace_autodetect()
    install_ui_anomaly()
    install_ui_vision()
    install_ui_skills()
    install_profiler_v066()

    _INSTALLED = True


def architecture_status() -> dict:
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
            "continuity", "doctor", "profiler", "self_repair", "perception",
            "context_intelligence", "workspace_autodetect", "anomaly_detection",
            "event_driven_vision", "skills",
        ],
        "compatibility_adapters": ["agent", "tools", "ui", "task_engine"],
        "versioned_runtime_chain": False,
    }
