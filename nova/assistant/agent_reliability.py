from __future__ import annotations

import re
import unicodedata
from typing import Any

from .experience_reliability import get_skill_reliability


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s._-]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def reliability_direct_intent(text: str) -> str | None:
    t = _normalize(text)
    if not t:
        return None
    if any(cue in t for cue in (
        "estado de fiabilidad", "estado de reliability", "experience reliability",
        "reliability loop", "fiabilidad de skills", "fiabilidad de habilidades",
    )):
        return "status"
    if any(cue in t for cue in (
        "habilidades que estan fallando", "skills que estan fallando", "habilidades fallando",
        "skills fallando", "habilidades obsoletas", "skills obsoletas", "habilidades degradadas",
        "skills degradadas", "que habilidades debo revisar", "que skills debo revisar",
    )):
        return "review"
    if "fiabilidad" in t or "reliability" in t:
        if "skill" in t or "habilidad" in t:
            return "report"
    return None


def _format_status(status: dict[str, Any]) -> str:
    return (
        f"Experience & Reliability Loop {'activo' if status.get('enabled') else 'desactivado'}.\n"
        f"Skills monitorizadas: {status.get('tracked_skills', 0)} · eventos: {status.get('events', 0)}.\n"
        f"Requieren revisión: {status.get('needs_review', 0)} "
        f"({status.get('degraded', 0)} degradadas · {status.get('stale', 0)} obsoletas por inactividad).\n"
        f"Ventana reciente: {status.get('rolling_window', 0)} ejecuciones · obsolescencia: {status.get('stale_days', 0)} días.\n"
        "Nova no deshabilita ni reescribe Skills automáticamente; una Skill verified puede volver a draft si acumula evidencia negativa."
    )


def _format_report(row: dict[str, Any]) -> str:
    if not row:
        return "No encontré información de fiabilidad para esa Skill."
    score = float(row.get("score", 0.5))
    lines = [
        f"Fiabilidad de «{row.get('skill_name') or '?'}» v{row.get('skill_version')}: {row.get('band')} · índice {score:.2f}/1.00.",
        f"Ventana reciente: {row.get('successes', 0)} correctas · {row.get('failures', 0)} fallidas · "
        f"{row.get('consecutive_failures', 0)} fallos consecutivos.",
        f"Motivo: {row.get('reason') or 'sin señales especiales'}.",
        f"Trust actual: {row.get('trust_level') or '?'}.",
    ]
    if row.get("needs_review"):
        lines.append("⚠ Conviene revisar/actualizar esta Skill antes de seguir confiando en el procedimiento actual.")
    return "\n".join(lines)


def _format_review(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No hay Skills degradadas u obsoletas que requieran revisión ahora mismo."
    lines = ["Skills que conviene revisar:"]
    for row in rows:
        lines.append(
            f"- {row.get('skill_name')} v{row.get('skill_version')} · {row.get('band')} · "
            f"índice {float(row.get('score',0)):.2f} · {row.get('reason')}"
        )
    lines.append("No las deshabilité ni modifiqué; la revisión del procedimiento sigue siendo explícita.")
    return "\n".join(lines)


def _extract_skill_query(text: str) -> str:
    t = str(text or "").strip()
    patterns = (
        r"(?i)fiabilidad\s+de\s+(?:la\s+)?(?:habilidad|skill)\s+(.+)$",
        r"(?i)reliability\s+de\s+(?:la\s+)?(?:habilidad|skill)\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, t)
        if match:
            return match.group(1).strip(" .?'\"")
    return ""


def install_agent_reliability():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_reliability_patched", False):
        return mod

    original_init = Agent.__init__
    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        registry = getattr(self, "skills", None)
        self.skill_reliability = get_skill_reliability(self.config, registry)

    def ask(self, user_text):
        action = reliability_direct_intent(str(user_text or ""))
        engine = getattr(self, "skill_reliability", None)
        if engine is None:
            engine = get_skill_reliability(self.config, getattr(self, "skills", None))
            self.skill_reliability = engine
        if action == "status":
            return _format_status(engine.status())
        if action == "review":
            return _format_review(engine.review_queue(20))
        if action == "report":
            query = _extract_skill_query(str(user_text or ""))
            row = engine.report(query) if query else {}
            if not row:
                registry = getattr(self, "skills", None)
                matches = registry.match(str(user_text or ""), limit=3) if registry is not None else []
                if matches:
                    row = engine.report(matches[0])
            return _format_report(row)
        return original_ask(self, user_text)

    def system_prompt(self):
        base = original_prompt(self) if callable(original_prompt) else ""
        engine = getattr(self, "skill_reliability", None)
        if engine is None:
            return base
        block = engine.compact_context()
        if not block:
            block = "(sin Skills degradadas/obsoletas relevantes)"
        return base + f"""

EXPERIENCE & RELIABILITY LOOP
{block}

REGLAS DE FIABILIDAD DE SKILLS
- El trust_level de una Skill no garantiza que siga funcionando en el entorno actual. Considera también su fiabilidad reciente.
- Una Skill `degraded` o `stale` requiere revalidación. No ocultes la advertencia ni asumas que un procedimiento antiguo sigue siendo correcto.
- No edites, deshabilites ni sustituyas silenciosamente una Skill por detectar degradación. Propón revisar/actualizar el playbook y conserva el historial.
- Los fallos recientes pesan más que la reputación histórica: una Skill previously verified puede volver a `draft` si aparece evidencia negativa.
- `skill_reliability.db` contiene metadatos de resultados, no prompts, argumentos, outputs ni contenido de los playbooks.
- Si una Skill falla porque cambió una versión de software, dependencia, ruta o interfaz, crea/actualiza una nueva versión solo después de comprobar el procedimiento corregido.
"""

    Agent.__init__ = init
    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_reliability_patched = True
    return mod
