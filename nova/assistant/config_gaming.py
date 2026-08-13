from __future__ import annotations

"""Defaults y migración de configuración para Gaming Awareness."""

from copy import deepcopy

from .gaming_awareness import DEFAULT_GAMING_CONFIG


def _merge_default_list(existing, defaults):
    out = []
    seen = set()
    for value in list(existing or []) + list(defaults or []):
        text = str(value or "").strip()
        key = text.casefold().replace("/", "\\")
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def install_config_gaming():
    # Los filtros amplían DEFAULT_GAMING_CONFIG con exclusiones de utilidades
    # antes de que la configuración persistente haga su migración.
    from .gaming_detection_filters import install_gaming_detection_filters
    install_gaming_detection_filters()

    from . import config as mod

    if getattr(mod, "_nova_gaming_awareness_patched", False):
        return mod

    mod.DEFAULT_CONFIG["gaming_awareness"] = deepcopy(DEFAULT_GAMING_CONFIG)
    original_load = mod.load_config

    def load_config():
        cfg = original_load()
        migrated = False
        gaming = cfg.get("gaming_awareness")
        if not isinstance(gaming, dict):
            cfg["gaming_awareness"] = deepcopy(DEFAULT_GAMING_CONFIG)
            migrated = True
        else:
            merged = deepcopy(DEFAULT_GAMING_CONFIG)
            merged.update(gaming)
            # Las nuevas exclusiones se añaden sin borrar exclusiones propias del
            # usuario. Esto permite que configs 0.9.7 hereden launchers/helpers.
            for key in ("ignored_game_processes", "ignored_game_path_markers"):
                merged[key] = _merge_default_list(gaming.get(key), DEFAULT_GAMING_CONFIG.get(key))
            policy = str(merged.get("release_policy") or "smart").casefold().strip()
            if policy not in {"smart", "always", "never"}:
                merged["release_policy"] = "smart"
            if merged != gaming:
                cfg["gaming_awareness"] = merged
                migrated = True
        if migrated:
            try:
                mod.save_config(cfg)
            except Exception:
                pass
        return cfg

    mod.load_config = load_config
    mod._nova_gaming_awareness_patched = True
    return mod
