from __future__ import annotations

import re
import unicodedata

from .profiler import get_profiler
from .self_repair import SelfRepairManager


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def performance_direct_intent(text: str) -> str | None:
    t = _normalize(text)
    if not t:
        return None
    if any(cue in t for cue in (
        "perfil de rendimiento", "performance profiler", "profiler de nova",
        "como va tu rendimiento", "como esta tu rendimiento", "muestra tu rendimiento",
        "por que estas lento", "por que vas lento", "cuello de botella", "cuellos de botella",
    )):
        return "performance"
    if any(cue in t for cue in (
        "que puedes reparar", "que puede reparar nova doctor", "reparaciones disponibles",
        "que puede arreglar doctor", "que puede arreglar nova doctor",
    )):
        return "repairs"
    if any(cue in t for cue in (
        "cual es tu atajo", "cual es el atajo de nova", "atajo global de nova",
        "hotkey de nova", "tecla para abrir nova",
    )):
        return "hotkey"
    return None


def install_agent_v066():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_v066_patched", False):
        return mod

    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def ask(self, user_text):
        action = performance_direct_intent(user_text)
        if action == "performance":
            try:
                profiler = get_profiler(self.config)
                return profiler.format_summary(profiler.summary(hours=24))
            except Exception as exc:
                return f"No pude leer el profiler local: {exc}"

        if action == "repairs":
            try:
                from .doctor import NovaDoctor
                report = NovaDoctor(self.config, self.memory).run()
                actions = SelfRepairManager(self.config, self.memory).available_actions(report)
                if not actions:
                    return "Nova Doctor no tiene reparaciones pendientes para los componentes que acaba de comprobar."
                lines = ["Nova Doctor puede ofrecer estas reparaciones:"]
                lines += [f"- {x.get('title')}: {x.get('detail')}" for x in actions[:10]]
                lines.append("Abre 🩺 Doctor para ejecutar cualquiera de ellas con confirmación explícita.")
                return "\n".join(lines)
            except Exception as exc:
                return f"No pude consultar las reparaciones de Nova Doctor: {exc}"

        if action == "hotkey":
            hotkey = str(self.config.get("hotkey", "<ctrl>+<alt>+<space>"))
            return f"El atajo global configurado para Nova es: {hotkey}. Los cambios de atajo se aplican al reiniciar Nova."

        return original_ask(self, user_text)

    def system_prompt(self):
        base = original_prompt(self) if callable(original_prompt) else ""
        return base + """

SELF REPAIR Y PROFILER
- Para investigar lentitud usa performance_summary antes de inventar una causa. Las métricas se almacenan solo localmente y no contienen prompts ni contenido de archivos.
- Nova Doctor puede proponer reparaciones deterministas, pero las reparaciones que instalan software/modelos o cambian componentes deben ejecutarse desde la UI con confirmación explícita.
- No afirmes que una reparación se realizó si SelfRepairManager no devolvió ok=true.
"""

    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_v066_patched = True
    return mod
