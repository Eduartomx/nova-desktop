from __future__ import annotations

"""Defaults y migración de configuración para Instant Wake y Resident Mode."""

from copy import deepcopy

from .hotkeys import DEFAULT_CONTEXT_HOTKEY, DEFAULT_MAIN_HOTKEY, normalize_hotkey
from .llm_warm import DEFAULT_LLM_WARM_CONFIG

DEFAULT_RESIDENT_CONFIG = {
    "enabled": True,
    "close_to_tray": True,
    "start_with_windows": False,
    "start_hidden": False,
    "notifications": True,
}


def install_config_instant_wake():
    from . import config as mod

    if getattr(mod, "_nova_instant_wake_patched", False):
        return mod

    mod.DEFAULT_CONFIG["hotkey"] = DEFAULT_MAIN_HOTKEY
    desktop = mod.DEFAULT_CONFIG.setdefault("desktop", {})
    desktop["context_hotkey"] = DEFAULT_CONTEXT_HOTKEY
    mod.DEFAULT_CONFIG["llm_warm"] = deepcopy(DEFAULT_LLM_WARM_CONFIG)
    mod.DEFAULT_CONFIG["resident_mode"] = deepcopy(DEFAULT_RESIDENT_CONFIG)

    original_load = mod.load_config

    def load_config():
        cfg = original_load()
        migrated = False

        main = normalize_hotkey(cfg.get("hotkey"), DEFAULT_MAIN_HOTKEY)
        if main in {"<ctrl>+<space>", "<ctrl>+<alt>+<space>"}:
            cfg["hotkey"] = DEFAULT_MAIN_HOTKEY
            migrated = True

        desktop_cfg = cfg.setdefault("desktop", {})
        context = normalize_hotkey(desktop_cfg.get("context_hotkey"), DEFAULT_CONTEXT_HOTKEY)
        if context in {"<ctrl>+<shift>+<space>", "<ctrl>+<alt>+<shift>+<space>"}:
            desktop_cfg["context_hotkey"] = DEFAULT_CONTEXT_HOTKEY
            migrated = True

        warm = cfg.get("llm_warm")
        if not isinstance(warm, dict):
            cfg["llm_warm"] = deepcopy(DEFAULT_LLM_WARM_CONFIG)
            migrated = True
        else:
            merged = deepcopy(DEFAULT_LLM_WARM_CONFIG)
            merged.update(warm)
            if merged != warm:
                cfg["llm_warm"] = merged
                migrated = True

        resident = cfg.get("resident_mode")
        if not isinstance(resident, dict):
            cfg["resident_mode"] = deepcopy(DEFAULT_RESIDENT_CONFIG)
            migrated = True
        else:
            merged_resident = deepcopy(DEFAULT_RESIDENT_CONFIG)
            merged_resident.update(resident)
            if merged_resident != resident:
                cfg["resident_mode"] = merged_resident
                migrated = True

        # La migración solo persiste preferencias. Nunca crea ni modifica la
        # entrada HKCU de inicio con Windows: esa acción requiere orden explícita.
        if migrated:
            try:
                mod.save_config(cfg)
            except Exception:
                pass
        return cfg

    mod.load_config = load_config
    mod._nova_instant_wake_patched = True
    return mod
