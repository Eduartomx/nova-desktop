from __future__ import annotations

"""Deterministic pre-LLM routing for Nova's own release/repository facts."""

import re
import unicodedata

from .action_context import bind_human_intent


def _normal(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    return " ".join("".join(ch for ch in raw if not unicodedata.combining(ch)).split())


_VERSION = ("que version eres", "que version tienes", "version de nova", "hay una actualizacion disponible")
_CHANGES = (
    "que cambio en la nueva version", "que se agrego en esta actualizacion", "muestrame tu changelog",
    "cuales son tus ultimos cambios", "que trae esta version", "que hay de nuevo",
    "cuales fueron los cambios", "que agregaron",
)
_ACTIVITY = (
    "consulta tu repositorio", "estado de tu repo", "actividad del repositorio", "estado de tu repositorio",
    "revisa tu github", "consulta tus commits",
)


def repository_route(text: str) -> str:
    raw = _normal(text)
    if any(cue in raw for cue in _CHANGES):
        return "changes"
    if any(cue in raw for cue in _VERSION):
        return "version"
    if any(cue in raw for cue in _ACTIVITY):
        return "activity"
    return ""


def _format_version(result: dict) -> str:
    current = result.get("current") or "desconocida"
    latest = result.get("latest") or "no disponible"
    if result.get("update_available") is True:
        state = f"Hay una actualización disponible: v{latest}."
    elif result.get("update_available") is False:
        state = "No hay una actualización pública más reciente."
    else:
        state = "No pude comprobar GitHub; no voy a inventar el estado remoto."
    return f"Tengo Nova v{current}. {state}\n\nEvidencia: {result.get('source')}; consultada {result.get('updated_at')}."


def _format_changes(result: dict) -> str:
    if not result.get("ok"):
        return f"No pude obtener el changelog. Evidencia: {result.get('source', 'GitHub no disponible')}."
    changes = str(result.get("changes") or "")
    bullets = [line for line in changes.splitlines() if line.lstrip().startswith("-")][:10]
    body = "\n".join(bullets) if bullets else changes[:2400]
    return f"Cambios de Nova v{result.get('version')}:\n\n{body}\n\nEvidencia: {result.get('source')}; consultada {result.get('updated_at')}."


def _format_activity(result: dict) -> str:
    rows = result.get("commits") or []
    if not rows:
        return f"No pude consultar actividad reciente. Evidencia: {result.get('source', 'GitHub no disponible')}."
    body = "\n".join(f"- {row.get('sha')}: {row.get('message')}" for row in rows[:8])
    return f"Actividad reciente del repositorio propio:\n\n{body}\n\nEvidencia: {result.get('source')}; consultada {result.get('updated_at')}."


def install_agent_repository():
    from .agent import LocalAgent
    if getattr(LocalAgent, "_nova_repository_routing", False):
        return LocalAgent
    original_ask = LocalAgent.ask

    def ask(self, user_text):
        route = repository_route(user_text)
        tools = getattr(self, "tools", None)
        intelligence = getattr(tools, "repository_intelligence", None)
        if route and intelligence is not None:
            try:
                self._last_fast_route = "version" if route == "version" else ("repository_changes" if route == "changes" else "repository_activity")
                with bind_human_intent(None):
                    if route == "version":
                        answer = _format_version(intelligence.version_status(refresh=True))
                    elif route == "changes":
                        answer = _format_changes(intelligence.whats_new(refresh=True))
                    else:
                        answer = _format_activity(intelligence.activity(limit=8))
                try:
                    self.memory.add_message("user", str(user_text or ""))
                    self.memory.add_message("assistant", answer)
                except Exception:
                    pass
                self._last_tool_trace = [{"name": "nova_" + ("whats_new" if route == "changes" else ("repository_activity" if route == "activity" else "version_status")), "ok": True, "authorization_state": "approved"}]
                return answer
            except Exception:
                pass
        return original_ask(self, user_text)

    LocalAgent.ask = ask
    LocalAgent._nova_repository_routing = True
    return LocalAgent
