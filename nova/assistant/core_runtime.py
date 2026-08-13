from __future__ import annotations

"""Bootstrap consolidado de Nova.

Desde v0.6.7 Memory/Workspace/Semantic Memory/Continuity y Nova Doctor son
módulos nativos administrados por GitHub. En v0.7 Perception Engine, Context
Intelligence, Workspace Auto-Detection, Anomaly Detection y Event-driven Vision
se suman como dominios nativos. En v0.8 Skills Engine añade playbooks locales,
Confidence Engine evalúa respaldo, Expert Escalation aporta segunda opinión y
Learn from Expert convierte soluciones verificadas en conocimiento reutilizable.
Agent/Tools/UI/TaskEngine todavía usan la base histórica local v0.5, por lo que
aquí se instalan únicamente adaptadores por dominio.
"""

_INSTALLED = False


def install_core_runtime():
    global _INSTALLED
    if _INSTALLED:
        return

    # Expert Resilience modifica defaults/migración y parchea el transporte antes
    # de que Tools/Agent/UI creen el singleton de Expert Escalation.
    from .expert_resilience import install_expert_resilience
    install_expert_resilience()

    # Herramientas por dominio. Learning se registra antes de Confidence para que
    # sus llamadas también queden observables por la instrumentación normal.
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
    from .tools_expert import install_tools_expert
    from .tools_learning import install_tools_learning
    from .tools_confidence import install_tools_confidence
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
    install_tools_expert()
    install_tools_learning()
    install_tools_confidence()

    # Confidence observa la petición normal completa; Expert reacciona al
    # assessment y Learning queda por fuera para capturar la opinión resultante.
    from .agent_workspace import install_agent_v060
    from .agent_semantic import install_agent_v063
    from .agent_continuity import install_agent_v065
    from .agent_diagnostics import install_agent_v066
    from .agent_perception import install_agent_perception
    from .agent_anomaly import install_agent_anomaly
    from .agent_vision import install_agent_vision
    from .agent_skills import install_agent_skills
    from .agent_confidence import install_agent_confidence
    from .agent_expert import install_agent_expert
    from .agent_learning import install_agent_learning
    install_agent_v060()
    install_agent_v063()
    install_agent_v065()
    install_agent_v066()
    install_agent_perception()
    install_agent_anomaly()
    install_agent_vision()
    install_agent_skills()
    install_agent_confidence()
    install_agent_expert()
    install_agent_learning()

    # UI y hooks del profiler al final.
    from .ui_workspace import install_ui_v060
    from .ui_semantic import install_ui_v063
    from .ui_continuity import install_ui_v065
    from .ui_diagnostics import install_ui_v066
    from .ui_perception import install_ui_perception
    from .ui_workspace_autodetect import install_ui_workspace_autodetect
    from .ui_anomaly import install_ui_anomaly
    from .ui_vision import install_ui_vision
    from .ui_skills import install_ui_skills
    from .ui_expert import install_ui_expert
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
    install_ui_expert()
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
            "event_driven_vision", "skills", "confidence", "expert_escalation",
            "expert_resilience", "learn_from_expert",
        ],
        "compatibility_adapters": ["agent", "tools", "ui", "task_engine"],
        "versioned_runtime_chain": False,
    }
