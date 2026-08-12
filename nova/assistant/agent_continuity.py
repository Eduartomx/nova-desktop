from __future__ import annotations

import re
import unicodedata
from typing import Any


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s#_-]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def continuity_direct_intent(text: str) -> str | None:
    """Routing determinista para peticiones inequívocas de continuidad."""
    t = _normalize(text)
    if not t:
        return None

    history_cues = (
        "que hicimos ayer", "que hicimos antes", "que hemos hecho", "historial del proyecto",
        "historial de este proyecto", "resume lo que hicimos", "resumen de lo que hicimos",
    )
    if any(cue in t for cue in history_cues):
        return "history"

    pending_cues = (
        "que quedo pendiente", "que queda pendiente", "que falta del proyecto", "pendientes del proyecto",
        "pendientes de este proyecto", "que nos falta", "que falta por hacer",
    )
    if any(cue in t for cue in pending_cues):
        return "pending"

    status_cues = (
        "donde nos quedamos", "en que quedamos", "por donde ibamos", "cual fue el ultimo paso",
        "ultimo checkpoint", "ultimo punto de control",
    )
    if any(cue in t for cue in status_cues):
        return "status"

    continue_cues = (
        "continua con lo de ayer", "continua con lo anterior", "continua donde quedamos",
        "continua donde nos quedamos", "continua el proyecto", "continua con el proyecto",
        "retoma lo de ayer", "retoma lo anterior", "retoma el proyecto", "retoma este proyecto",
        "sigue con lo de ayer", "sigue donde quedamos", "sigue donde nos quedamos",
    )
    if t in {"continua", "continuar", "retoma", "retomar", "sigue"} or any(cue in t for cue in continue_cues):
        return "continue"

    return None


def _format_resume(state: dict[str, Any]) -> str:
    if not state.get("ok"):
        return "No tengo un checkpoint, sesión o tarea abierta suficiente para reconstruir dónde nos quedamos."
    workspace = state.get("workspace")
    compact = str(state.get("compact") or "").strip()
    head = f"Continuidad de {workspace}:" if workspace else "Último estado de continuidad:"
    return head + ("\n" + compact if compact else "")


def _format_pending(state: dict[str, Any]) -> str:
    if not state.get("ok"):
        return "No encontré trabajo pendiente registrado."
    items = list(state.get("pending_items") or [])
    workspace = state.get("workspace")
    if not items:
        return f"No hay pendientes registrados{' en ' + workspace if workspace else ''}."
    lines = [f"Pendientes{' de ' + workspace if workspace else ''}:"]
    lines += [f"- {item}" for item in items[:12]]
    return "\n".join(lines)


def _format_history(result: dict[str, Any]) -> str:
    rows = list(result.get("history") or [])
    workspace = result.get("workspace")
    if not rows:
        return "No encontré checkpoints anteriores para reconstruir el historial."
    lines = [f"Historial reciente{' de ' + workspace if workspace else ''}:"]
    for row in rows[-10:]:
        when = str(row.get("created_at") or "").strip()
        summary = str(row.get("summary") or row.get("kind") or "checkpoint").strip()
        pending = row.get("pending") or []
        suffix = f" · pendiente: {pending[0]}" if pending else ""
        lines.append(f"- {when}: {summary}{suffix}")
    return "\n".join(lines)


def install_agent_v065():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_v065_patched", False):
        return mod

    original_init = Agent.__init__
    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._continuity_context_override = ""
        try:
            self.memory.configure_continuity(self.config.get("continuity", {}))
        except Exception:
            pass

    def _resume_via_tools(self):
        tools = getattr(self, "tools", None)
        if tools is not None and hasattr(tools, "continuity_resume"):
            return tools.continuity_resume()
        active = self.memory.active_workspace()
        wid = int(active["id"]) if active else None
        result = self.memory.continuity_resume(workspace_id=wid, any_if_none=active is None)
        result["workspace"] = active.get("name") if active else None
        return result

    def ask(self, user_text):
        action = continuity_direct_intent(user_text)
        if not action:
            return original_ask(self, user_text)

        try:
            self._current_user_text = user_text or ""
        except Exception:
            pass

        tools = getattr(self, "tools", None)
        try:
            if action == "pending":
                if tools is not None and hasattr(tools, "continuity_pending"):
                    return _format_pending(tools.continuity_pending())
                active = self.memory.active_workspace()
                wid = int(active["id"]) if active else None
                result = self.memory.continuity_pending(workspace_id=wid, any_if_none=active is None)
                result["workspace"] = active.get("name") if active else None
                return _format_pending(result)

            if action == "history":
                if tools is not None and hasattr(tools, "continuity_history"):
                    result = tools.continuity_history(limit=12)
                else:
                    active = self.memory.active_workspace()
                    wid = int(active["id"]) if active else None
                    rows = self.memory.continuity_history(workspace_id=wid, limit=12, any_if_none=active is None)
                    result = {"ok": bool(rows), "workspace": active.get("name") if active else None, "history": rows}
                return _format_history(result)

            state = _resume_via_tools(self)
            if action == "status":
                return _format_resume(state)

            # "Nova, continúa" necesita razonar/actuar, pero ya con el estado local
            # reconstruido. Inyectamos el checkpoint antes de entrar al agente normal.
            if not state.get("ok"):
                return _format_resume(state)
            self._continuity_context_override = str(state.get("compact") or "").strip()
            try:
                return original_ask(self, user_text)
            finally:
                self._continuity_context_override = ""
        except Exception as exc:
            self._continuity_context_override = ""
            return f"No pude reconstruir la continuidad de trabajo: {exc}"

    def system_prompt(self):
        base = original_prompt(self) if callable(original_prompt) else ""
        cfg = self.config.get("continuity", {}) if isinstance(self.config, dict) else {}
        if not cfg.get("enabled", True) or not cfg.get("inject_context", True):
            return base

        context = str(getattr(self, "_continuity_context_override", "") or "").strip()
        if not context:
            try:
                active = self.memory.active_workspace()
                if active:
                    state = self.memory.continuity_resume(workspace_id=int(active["id"]), any_if_none=False)
                    if state.get("ok"):
                        context = str(state.get("compact") or "").strip()
            except Exception:
                context = ""
        if not context:
            context = "(sin sesión de continuidad activa)"
        if len(context) > 2200:
            context = context[:2200] + "…"

        return base + f"""

CONTINUITY ENGINE
{context}

Reglas de continuidad:
- Si el usuario dice «continúa», «retoma», «dónde nos quedamos» o «qué quedó pendiente», usa primero Continuity Engine; no reconstruyas el estado inventándolo desde conversación genérica.
- Un checkpoint representa estado temporal/accionable. Semantic Memory representa hechos y decisiones duraderas. No mezcles ambos indiscriminadamente.
- Cuando una tarea cambia a completada, fallida, pausada o bloqueada, el sistema crea checkpoints automáticamente.
- Antes de abandonar una tarea compleja, usa continuity_checkpoint si hay estado útil que deba sobrevivir al reinicio.
- Al continuar, respeta los pasos ya completados y empieza por el primer pendiente verificable. No repitas trabajo terminado salvo que necesites validarlo.
"""

    Agent.__init__ = init
    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_v065_patched = True
    return mod
