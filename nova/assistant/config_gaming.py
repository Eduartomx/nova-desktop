from __future__ import annotations

"""Defaults y migración de configuración para Nova 0.9.5."""

from copy import deepcopy

from .gaming_awareness import DEFAULT_GAMING_CONFIG


def install_config_gaming():
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
