from __future__ import annotations

import re
import unicodedata

from .llm_benchmark import format_llm_benchmark, run_llm_benchmark
from .llm_performance import get_llm_performance
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
        "prueba tu rendimiento", "prueba el rendimiento de qwen", "prueba el rendimiento de ollama",
        "benchmark de nova", "benchmark de qwen", "benchmark de ollama", "benchmark del llm",
        "haz un benchmark", "ejecuta el benchmark",
    )):
        return "benchmark"
    if any(cue in t for cue in (
        "rendimiento de qwen", "rendimiento de ollama", "rendimiento del llm", "performance del llm",
        "por que qwen tarda", "por que ollama tarda", "por que el llm tarda",
        "metricas de qwen", "metricas de ollama", "metricas del llm",
        "tokens por segundo", "tok s", "cold start de qwen", "cold start de ollama",
    )):
        return "llm_performance"
    if any(cue in t for cue in (
        "perfil de rendimiento", "performance profiler", "profiler de nova",
        "como va tu rendimiento", "como esta tu rendimiento", "muestra tu rendimiento",
        "por que estas lento", "por que vas lento",
        "cuello de botella de nova", "cuellos de botella de nova",
        "que te esta haciendo lento", "que hace lenta a nova",
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


def performance_window(text: str) -> tuple[float, bool, str]:
    t = _normalize(text)
    if "sesion" in t or "desde que abriste" in t or "desde que iniciaste" in t:
        return 24 * 30, True, "sesión actual"
    if ("15" in t and ("min" in t or "minuto" in t)) or "ultimo cuarto de hora" in t:
        return 0.25, False, "últimos 15 min"
    if "ultima hora" in t or "ultimos 60" in t or "1 hora" in t:
        return 1.0, False, "última hora"
    if "24" in t and ("hora" in t or "h" in t):
        return 24.0, False, "últimas 24 h"
    # En consultas interactivas priorizamos la sesión para no mezclar versiones
    # antiguas o mediciones previas a una actualización.
    return 24 * 30, True, "sesión actual"


def install_agent_v066():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_v066_patched", False):
        return mod

    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def ask(self, user_text):
        action = performance_direct_intent(user_text)
        if action == "benchmark":
            try:
                return format_llm_benchmark(run_llm_benchmark(self))
            except Exception as exc:
                return f"No pude completar el benchmark local de Ollama: {type(exc).__name__}: {exc}"

        if action == "llm_performance":
            try:
                hours, session_only, label = performance_window(user_text)
                monitor = get_llm_performance(self.config)
                report = monitor.summary(hours=hours, session_only=session_only)
                return monitor.format_summary(report, title=f"Rendimiento LLM · {label}")
            except Exception as exc:
                return f"No pude leer las métricas locales del LLM: {exc}"

        if action == "performance":
            try:
                hours, session_only, label = performance_window(user_text)
                profiler = get_profiler(self.config)
                report = profiler.summary(hours=hours, session_only=session_only)
                return profiler.format_summary(report, title=f"Rendimiento de Nova · {label}")
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

SELF REPAIR Y PERFORMANCE INTELLIGENCE
- Para investigar lentitud usa performance_summary y llm_performance_summary antes de inventar una causa. Las métricas son locales y no contienen prompts ni respuestas.
- Las métricas de Ollama separan carga del modelo, evaluación del prompt y generación. No atribuyas automáticamente toda demora a la GPU o a un juego.
- `Nova, prueba tu rendimiento` ejecuta un benchmark local explícito y acotado; nunca lo lances automáticamente en segundo plano.
- Nova Doctor puede proponer reparaciones deterministas, pero las reparaciones que instalan software/modelos o cambian componentes deben ejecutarse desde la UI con confirmación explícita.
- No afirmes que una reparación se realizó si SelfRepairManager no devolvió ok=true.
"""

    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_v066_patched = True
    return mod
