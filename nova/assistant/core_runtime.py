from __future__ import annotations

"""Bootstrap consolidado de Nova.

Desde v0.9.0 Agent, LocalTools, AssistantUI y TaskEngine también viven en
GitHub. Browser Agent, control estructurado del escritorio, backups de escritura
y activación local por wake word forman parte del núcleo administrado. Las capas
`agent_*`, `tools_*` y `ui_*` se conservan temporalmente por dominio mientras
0.9.x absorbe su comportamiento.
"""

_INSTALLED = False


def install_core_runtime():
    global _INSTALLED
    if _INSTALLED:
        return

    # Deben instalarse antes de que app.py importe load_config.
    from .config_instant_wake import install_config_instant_wake
    install_config_instant_wake()
    from .config_gaming import install_config_gaming
    install_config_gaming()

    # Gaming Mode puede reducir temporalmente el polling de Perception sin
    # reiniciar su hilo ni cambiar el valor persistente del usuario.
    from .perception_gaming import install_perception_gaming
    install_perception_gaming()
    from .gaming_status_sync import install_gaming_status_sync
    install_gaming_status_sync()

    from .expert_resilience import install_expert_resilience
    install_expert_resilience()

    from .experience_reliability import install_skill_reliability_hooks
    install_skill_reliability_hooks()

    from .tools_desktop import install_tools_desktop
    from .tools_file_safety import install_tools_file_safety
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
    from .tools_reliability import install_tools_reliability
    from .tools_expert import install_tools_expert
    from .tools_learning import install_tools_learning
    from .tools_confidence import install_tools_confidence
    install_tools_desktop()
    install_tools_file_safety()
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
    install_tools_reliability()
    install_tools_expert()
    install_tools_learning()
    install_tools_confidence()

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
    from .agent_reliability import install_agent_reliability
    from .agent_fast_routing import install_agent_fast_routing
    from .agent_instant_wake import install_agent_instant_wake
    from .agent_gaming import install_agent_gaming
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
    install_agent_reliability()
    # Fast Routing sigue por fuera del pipeline de dominios. Instant Wake añade
    # keep_alive y Gaming Awareness envuelve finalmente los comandos directos y
    # la política temporal de VRAM.
    install_agent_fast_routing()
    install_agent_instant_wake()
    install_agent_gaming()

    from .ui_workspace import install_ui_v060
    from .ui_semantic import install_ui_v063
    from .ui_continuity import install_ui_v065
    from .ui_diagnostics import install_ui_v066
    from .ui_perception import install_ui_perception
    from .ui_workspace_autodetect import install_ui_workspace_autodetect
    from .ui_anomaly import install_ui_anomaly
    from .ui_vision import install_ui_vision
    from .ui_skills import install_ui_skills
    from .ui_reliability import install_ui_reliability
    from .ui_expert import install_ui_expert
    from .ui_voice_wake import install_ui_voice_wake
    from .ui_instant_wake import install_ui_instant_wake
    from .ui_gaming import install_ui_gaming
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
    install_ui_reliability()
    install_ui_expert()
    install_ui_voice_wake()
    install_ui_instant_wake()
    # Gaming Awareness va después de Instant Wake porque coordina su Warm
    # Manager y añade estado visual/Doctor sobre la UI final.
    install_ui_gaming()
    install_profiler_v066()

    _INSTALLED = True


def architecture_status() -> dict:
    import importlib.util

    core_modules = {
        "agent": "assistant.agent",
        "tools": "assistant.tools",
        "ui": "assistant.ui",
        "task_engine": "assistant.task_engine",
    }
    managed_core = {name: importlib.util.find_spec(module) is not None for name, module in core_modules.items()}
    native_domains = [
        "memory", "workspace", "workspace_index", "semantic_memory",
        "continuity", "doctor", "profiler", "self_repair", "perception",
        "context_intelligence", "workspace_autodetect", "anomaly_detection",
        "event_driven_vision", "skills", "confidence", "expert_escalation",
        "expert_resilience", "learn_from_expert", "experience_reliability",
        "desktop_browser_control", "file_write_safety", "voice_wake",
        "fast_routing", "adaptive_memory_context", "llm_performance_intelligence", "llm_benchmark",
        "llm_warm_manager", "instant_wake", "configurable_hotkeys",
        "gaming_awareness", "gaming_vram_policy", "gaming_perception_throttle", "gaming_state_sync",
        "agent", "tools", "ui", "task_engine",
    ]
    return {
        "ok": all(managed_core.values()),
        "bootstrap": "assistant.core_runtime",
        "github_managed_core": managed_core,
        "legacy_local_contract": {},
        "github_managed_native": native_domains,
        "compatibility_adapters": ["agent_domain", "tools_domain", "ui_domain"],
        "unmanaged_core_files": [],
        "versioned_runtime_chain": False,
    }
