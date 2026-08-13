from __future__ import annotations

import re
import unicodedata
from typing import Any

from .confidence import get_confidence_engine


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def confidence_direct_intent(text: str) -> str | None:
    t = _normalize(text)
    if not t:
        return None
    if any(cue in t for cue in (
        "estado del confidence engine", "estado de confianza", "motor de confianza",
        "esta funcionando tu confianza", "como funciona tu confianza",
    )):
        return "status"
    if any(cue in t for cue in (
        "historial de confianza", "confianzas recientes", "evaluaciones de confianza recientes",
    )):
        return "recent"
    if any(cue in t for cue in (
        "que tan seguro estas", "estas seguro", "cuanta confianza tienes", "confianza en esa respuesta",
        "por que no estas seguro", "por que tienes poca confianza", "ultima confianza", "ultima evaluacion de confianza",
    )):
        return "last"
    return None


def _band_es(value: str) -> str:
    return {"high": "alta", "medium": "media", "low": "baja"}.get(str(value), str(value or "desconocida"))


def format_assessment(row: dict[str, Any]) -> str:
    if not row:
        return "Todavía no tengo una evaluación de confianza anterior."
    reasons = list(row.get("reason_codes") or [])
    reason_map = {
        "structured_evidence": "hay evidencia estructurada",
        "verified_evidence": "hubo verificación posterior",
        "tool_failures": "una o más herramientas fallaron",
        "contradictions": "las evidencias se contradicen",
        "deterministic_route": "la ruta fue determinista",
        "no_structured_evidence": "faltó evidencia estructurada",
        "risk_high": "la acción tiene impacto alto",
        "risk_critical": "la acción es crítica",
        "skill_verified": "se reutilizó una Skill verificada",
        "skill_user": "se reutilizó una Skill definida por el usuario",
        "skill_draft": "la Skill todavía está en draft",
    }
    readable = [reason_map.get(x, x.replace("_", " ")) for x in reasons[:8]]
    lines = [
        f"Confianza {_band_es(row.get('band'))} · índice heurístico {float(row.get('score', 0)):.2f}/1.00.",
        "Ese índice NO es una probabilidad calibrada de que la respuesta sea correcta.",
        f"Evidencia estructurada/verificada: {row.get('evidence_count', 0)} · fallos: {row.get('failure_count', 0)} · contradicciones: {row.get('contradiction_count', 0)}.",
    ]
    if readable:
        lines.append("Motivos: " + "; ".join(readable) + ".")
    if row.get("escalation_candidate"):
        lines.append("Esta petición sería candidata para una segunda opinión asistida cuando habilitemos Expert Escalation.")
    return "\n".join(lines)


def _known_deterministic_route(text: str) -> bool:
    """Reconoce rutas locales que no pasan por generación libre del modelo."""
    checks = []
    try:
        from .agent_skills import skills_direct_intent
        action = skills_direct_intent(text)
        checks.append(action in {"status", "list", "runs"})
    except Exception:
        pass
    try:
        from .agent_anomaly import anomaly_direct_intent
        checks.append(anomaly_direct_intent(text) in {"status", "recent", "ack_all", "mark_current_expected"})
    except Exception:
        pass
    try:
        from .agent_vision import vision_direct_intent
        checks.append(vision_direct_intent(text) in {"status", "last", "recent"})
    except Exception:
        pass
    try:
        from .agent_continuity import continuity_direct_intent
        checks.append(continuity_direct_intent(text) in {"status", "pending", "history"})
    except Exception:
        pass
    try:
        from .agent_perception import perception_direct_intent
        checks.append(perception_direct_intent(text) is not None)
    except Exception:
        pass
    return any(checks)


def _explicit_skill_trust(agent, text: str) -> str:
    try:
        from .agent_skills import skills_direct_intent
        if skills_direct_intent(text) != "run":
            return ""
        registry = getattr(agent, "skills", None)
        if registry is None:
            return ""
        matches = registry.match(str(text or ""), limit=3)
        threshold = float(registry.config.get("explicit_run_threshold", 0.58))
        best = next((x for x in matches if float(x.get("match_score", 0)) >= threshold), None)
        return str((best or {}).get("trust_level") or "")
    except Exception:
        return ""


def install_agent_confidence():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_confidence_patched", False):
        return mod

    original_init = Agent.__init__
    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.confidence = get_confidence_engine(self.config, getattr(self, "memory", None))

    def ask(self, user_text):
        engine = getattr(self, "confidence", None) or get_confidence_engine(self.config, getattr(self, "memory", None))
        action = confidence_direct_intent(user_text)
        if action == "status":
            status = engine.status()
            return (
                f"Confidence Engine {'activo' if status.get('enabled') else 'desactivado'} · "
                f"{status.get('assessments', 0)} evaluaciones registradas · "
                f"{status.get('escalation_candidates', 0)} candidatas a segunda opinión. "
                "No usa la confianza autodeclarada por Qwen y no guarda prompts ni respuestas. "
                "Sus índices son heurísticos, no probabilidades calibradas."
            )
        if action == "last":
            return format_assessment(engine.last())
        if action == "recent":
            rows = engine.recent(12)
            if not rows:
                return "Todavía no hay evaluaciones de confianza registradas."
            lines = ["Evaluaciones recientes de confianza:"]
            for row in rows:
                lines.append(
                    f"- {row.get('created_at')} · {_band_es(row.get('band'))} · índice {float(row.get('score',0)):.2f} · "
                    f"{row.get('request_kind')} · evidencia {row.get('evidence_count',0)} · fallos {row.get('failure_count',0)}"
                )
            lines.append("Los índices son heurísticos; no representan probabilidades calibradas.")
            return "\n".join(lines)

        if not engine.enabled:
            return original_ask(self, user_text)

        engine.begin_request(str(user_text or ""), skill_trust=_explicit_skill_trust(self, str(user_text or "")))
        if _known_deterministic_route(str(user_text or "")):
            engine.mark_deterministic("known_local_direct_route")

        try:
            result = original_ask(self, user_text)
        except Exception:
            assessment = engine.finish_request(response_ok=False)
            self._last_confidence_assessment = assessment
            raise

        assessment = engine.finish_request(response_ok=True)
        self._last_confidence_assessment = assessment
        if (
            assessment.get("escalation_candidate")
            and self.config.get("confidence", {}).get("surface_low_confidence", True)
            and assessment.get("band") == "low"
        ):
            notice = (
                "\n\n⚠ Confianza baja: esta respuesta no quedó suficientemente respaldada por evidencia/"
                "verificación local. El índice de Confidence Engine es heurístico, no una probabilidad."
            )
            return str(result or "") + notice
        return result

    def system_prompt(self):
        base = original_prompt(self) if callable(original_prompt) else ""
        cfg = self.config.get("confidence", {}) if isinstance(self.config, dict) else {}
        if not cfg.get("enabled", True):
            return base
        return base + """

CONFIDENCE ENGINE
REGLAS DE CONFIANZA
- No uses frases como «estoy 95% seguro» basadas únicamente en intuición del modelo. La autoconfianza del LLM NO es evidencia.
- Confidence Engine combina señales deterministas: resultados de herramientas, lecturas estructuradas, verificaciones, fallos, contradicciones, riesgo y confianza de Skills.
- El índice 0..1 es una HEURÍSTICA de respaldo, no una probabilidad calibrada de corrección.
- Para diagnóstico, estado actual, hechos verificables y acciones de impacto alto, prefiere obtener evidencia estructurada antes de afirmar algo con seguridad.
- Si herramientas/evidencias se contradicen, dilo y busca una comprobación adicional en vez de ocultar la discrepancia.
- Una Skill `verified` aporta evidencia histórica, pero nunca sustituye verificar que el contexto actual siga siendo compatible.
- Cuando Confidence Engine marque una petición como candidata a escalación, no inventes certeza para evitar pedir ayuda. En v0.8.1 solo se registra la candidatura; la consulta asistida a ChatGPT llegará en la siguiente etapa.
"""

    Agent.__init__ = init
    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_confidence_patched = True
    return mod
