from __future__ import annotations

"""Comandos deterministas de Gaming Awareness para el Agent."""

import re
import unicodedata

from .config import save_config
from .gaming_awareness import get_gaming_awareness


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def gaming_direct_intent(text: str) -> str | None:
    t = _normalize(text)
    if not t:
        return None
    if any(cue in t for cue in (
        "manten qwen cargado aunque juegue", "manten qwen cargado aunque este jugando",
        "no liberes qwen al jugar", "no liberes qwen cuando juegue",
        "no descargues qwen al jugar", "quiero qwen cargado mientras juego",
    )):
        return "keep_llm"
    if any(cue in t for cue in (
        "libera qwen al jugar", "libera qwen cuando juegue", "prioriza la vram al jugar",
        "prioriza vram al jugar", "vuelve a liberar qwen al jugar", "libera el llm durante juegos",
    )):
        return "release_llm"
    if any(cue in t for cue in (
        "activa modo juego", "activar modo juego", "entra en modo juego",
        "fuerza modo juego", "enciende modo juego", "gaming mode on",
    )):
        return "on"
    if any(cue in t for cue in (
        "desactiva modo juego", "desactivar modo juego", "sal del modo juego",
        "apaga modo juego", "modo normal", "gaming mode off",
    )):
        return "off"
    if any(cue in t for cue in (
        "modo juego automatico", "modo juego en automatico", "deteccion automatica de juegos",
        "detecta juegos automaticamente", "gaming mode auto", "vuelve a modo juego automatico",
    )):
        return "auto"
    if any(cue in t for cue in (
        "estado del modo juego", "estado de modo juego", "estas en modo juego",
        "gaming awareness", "gaming mode", "modo juego", "por que liberaste qwen",
        "por que descargaste qwen", "que juego detectaste",
    )):
        return "status"
    return None


def install_agent_gaming():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_gaming_awareness_patched", False):
        return mod

    original_init = Agent.__init__
    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.gaming_awareness = get_gaming_awareness(self.config)

    def ask(self, user_text):
        action = gaming_direct_intent(user_text)
        if action:
            manager = getattr(self, "gaming_awareness", None) or get_gaming_awareness(self.config)
            if action in {"on", "off", "auto"}:
                report = manager.set_mode(action)
                prefix = {
                    "on": "Activé Gaming Mode manualmente.",
                    "off": "Desactivé Gaming Mode manualmente.",
                    "auto": "Gaming Mode volvió a detección automática.",
                }[action]
                return prefix + "\n\n" + manager.format_status(report)
            if action == "keep_llm":
                report = manager.set_keep_llm_loaded(True)
                try:
                    save_config(self.config)
                except Exception:
                    pass
                return "Mantendré Qwen cargado durante los juegos hasta que cambies esta preferencia.\n\n" + manager.format_status(report)
            if action == "release_llm":
                report = manager.set_keep_llm_loaded(False)
                try:
                    save_config(self.config)
                except Exception:
                    pass
                return "Gaming Mode volverá a priorizar la VRAM del juego.\n\n" + manager.format_status(report)
            return manager.format_status(manager.status(refresh=True))
        return original_ask(self, user_text)

    def system_prompt(self):
        base = original_prompt(self) if callable(original_prompt) else ""
        return base + """

GAMING AWARENESS
- Gaming Awareness usa metadatos locales de Perception y procesos; no inspecciona memoria del juego, no inyecta código y no captura pantalla para detectar juegos.
- En modo automático puede reducir la frecuencia de Perception y liberar Qwen cuando un juego necesita margen de VRAM.
- La política smart prioriza un juego en primer plano o presión real de VRAM; no atribuyas una descarga a Gaming Mode si el estado no lo confirma.
- Si Qwen se usa mientras Gaming Mode está activo, la inferencia puede ejecutarse y después usar keep_alive reducido; nunca interrumpas una inferencia activa.
- El usuario puede forzar modo juego, desactivarlo o pedir mantener Qwen cargado durante juegos.
"""

    Agent.__init__ = init
    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_gaming_awareness_patched = True
    return mod
