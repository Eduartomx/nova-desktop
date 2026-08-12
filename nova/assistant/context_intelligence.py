from __future__ import annotations

"""Context Intelligence para Nova.

Convierte las señales crudas de Perception Engine en un contexto pequeño y útil.
No usa LLM, no captura contenido adicional y no ejecuta acciones.
"""

from collections import Counter
from typing import Any


DEFAULT_CONTEXT_INTELLIGENCE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "recent_event_limit": 32,
    "prompt_event_limit": 4,
    "minimum_prompt_relevance": 0.30,
    "workspace_confidence_bonus": 0.22,
    "system_pressure_bonus": 0.38,
    "app_change_bonus": 0.16,
    "workspace_change_bonus": 0.24,
    "include_window_title_in_prompt": False,
}

_ACTIVITY_LABELS = {
    "programming": "programando/desarrollando",
    "coding": "editando código",
    "terminal_work": "trabajando en terminal",
    "research": "investigando entre navegador y herramientas de trabajo",
    "browsing": "navegando por la web",
    "file_management": "administrando archivos",
    "gaming": "jugando",
    "communication": "comunicándose",
    "media": "usando contenido multimedia",
    "office_work": "trabajando con documentos/ofimática",
    "java_app": "usando una aplicación Java",
    "other": "usando el escritorio",
    "unknown": "sin actividad suficiente para inferir",
}

_EVENT_WEIGHTS = {
    "cpu_pressure": 1.0,
    "memory_pressure": 1.0,
    "workspace_candidate": 0.72,
    "workspace_changed": 0.82,
    "app_changed": 0.50,
    "context_started": 0.30,
    "cpu_recovered": 0.45,
    "memory_recovered": 0.45,
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _event_signature(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(event.get("event_type") or ""),
        str(event.get("process_name") or event.get("process") or "").casefold(),
        str(event.get("app_kind") or "").casefold(),
        event.get("workspace_id"),
    )


def condense_events(events: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """Reduce rebotes repetitivos conservando cambios distintos y relevantes."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw in events or []:
        event = dict(raw)
        signature = _event_signature(event)
        if signature in seen:
            continue
        seen.add(signature)
        event["relevance"] = round(_EVENT_WEIGHTS.get(str(event.get("event_type") or ""), 0.20), 3)
        out.append(event)
        if len(out) >= max(1, int(limit)):
            break
    return out


def infer_activity(state: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    external = state.get("external") if isinstance(state.get("external"), dict) else {}
    current_kind = str(external.get("app_kind") or "other")
    recent_kinds = [
        str(row.get("app_kind") or "")
        for row in (events or [])[:16]
        if str(row.get("app_kind") or "")
    ]
    kinds = Counter(recent_kinds)

    activity = "other"
    confidence = 0.55
    reasons: list[str] = []

    if current_kind == "code_editor":
        if kinds.get("terminal", 0) > 0:
            activity, confidence = "programming", 0.92
            reasons.append("editor + terminal recientes")
        elif kinds.get("browser", 0) > 0:
            activity, confidence = "research", 0.82
            reasons.append("editor + navegador recientes")
        else:
            activity, confidence = "coding", 0.86
            reasons.append("editor de código activo")
    elif current_kind == "terminal":
        if kinds.get("code_editor", 0) > 0:
            activity, confidence = "programming", 0.90
            reasons.append("terminal + editor recientes")
        else:
            activity, confidence = "terminal_work", 0.80
            reasons.append("terminal activa")
    elif current_kind == "browser":
        if kinds.get("code_editor", 0) > 0 or kinds.get("terminal", 0) > 0:
            activity, confidence = "research", 0.80
            reasons.append("navegador alternando con herramientas técnicas")
        else:
            activity, confidence = "browsing", 0.72
            reasons.append("navegador activo")
    elif current_kind == "explorer":
        activity, confidence = "file_management", 0.78
        reasons.append("Explorador de archivos activo")
    elif current_kind == "game":
        activity, confidence = "gaming", 0.96
        reasons.append("juego activo")
    elif current_kind == "communication":
        activity, confidence = "communication", 0.86
        reasons.append("aplicación de comunicación activa")
    elif current_kind == "media":
        activity, confidence = "media", 0.82
        reasons.append("aplicación multimedia activa")
    elif current_kind == "office":
        activity, confidence = "office_work", 0.86
        reasons.append("aplicación de ofimática activa")
    elif current_kind == "java_app":
        activity, confidence = "java_app", 0.70
        reasons.append("aplicación Java activa")
    elif not external:
        activity, confidence = "unknown", 0.15
        reasons.append("sin ventana externa observada")

    probable = state.get("probable_workspace") if isinstance(state.get("probable_workspace"), dict) else None
    if probable and _as_float(probable.get("confidence")) >= 0.78:
        confidence = min(0.99, confidence + 0.04)
        reasons.append(f"workspace probable: {probable.get('name')}")

    return {
        "activity": activity,
        "label": _ACTIVITY_LABELS.get(activity, activity),
        "confidence": round(confidence, 3),
        "reasons": reasons[:4],
        "app_kind": current_kind,
        "process": external.get("process") or "",
    }


def score_relevance(state: dict[str, Any], events: list[dict[str, Any]] | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONTEXT_INTELLIGENCE_CONFIG)
    if isinstance(config, dict):
        cfg.update(config)

    score = 0.08
    reasons: list[str] = []
    probable = state.get("probable_workspace") if isinstance(state.get("probable_workspace"), dict) else None
    active = state.get("active_workspace") if isinstance(state.get("active_workspace"), dict) else None
    system = state.get("system") if isinstance(state.get("system"), dict) else {}

    if probable:
        confidence = _as_float(probable.get("confidence"))
        if confidence >= 0.78:
            bonus = _as_float(cfg.get("workspace_confidence_bonus"), 0.22) * min(1.0, confidence)
            score += bonus
            reasons.append("workspace probable con confianza alta")
        if active and str(active.get("id")) == str(probable.get("id")):
            score += 0.08
            reasons.append("workspace probable coincide con el activo")

    cpu = _as_float(system.get("cpu_percent"))
    memory = _as_float(system.get("memory_percent"))
    if cpu >= 92 or memory >= 90:
        score += _as_float(cfg.get("system_pressure_bonus"), 0.38)
        reasons.append("presión alta de recursos")

    condensed = condense_events(list(events or []), 8)
    event_types = {str(row.get("event_type") or "") for row in condensed}
    if "workspace_candidate" in event_types or "workspace_changed" in event_types:
        score += _as_float(cfg.get("workspace_change_bonus"), 0.24)
        reasons.append("cambio de contexto de proyecto")
    if "app_changed" in event_types:
        score += _as_float(cfg.get("app_change_bonus"), 0.16)
        reasons.append("cambio de aplicación")
    if "cpu_pressure" in event_types or "memory_pressure" in event_types:
        score += 0.22
        reasons.append("evento reciente de presión del sistema")

    score = min(1.0, max(0.0, score))
    threshold = _as_float(cfg.get("minimum_prompt_relevance"), 0.30)
    return {
        "score": round(score, 3),
        "relevant": score >= threshold,
        "threshold": round(threshold, 3),
        "reasons": reasons[:5],
    }


class ContextIntelligence:
    def __init__(self, perception_engine, config: dict[str, Any] | None = None):
        self.engine = perception_engine
        self.config = dict(DEFAULT_CONTEXT_INTELLIGENCE_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def snapshot(self, refresh: bool = False) -> dict[str, Any]:
        state = self.engine.current(refresh=bool(refresh)) if self.engine is not None else {}
        limit = max(4, int(self.config.get("recent_event_limit", 32)))
        try:
            events = list(self.engine.recent_events(limit)) if self.engine is not None else []
        except Exception:
            events = []
        activity = infer_activity(state, events)
        relevance = score_relevance(state, events, self.config)
        important = sorted(
            condense_events(events, max(1, int(self.config.get("prompt_event_limit", 4)))) ,
            key=lambda row: float(row.get("relevance") or 0),
            reverse=True,
        )
        return {
            "ok": True,
            "enabled": self.enabled,
            "state": state,
            "activity": activity,
            "relevance": relevance,
            "important_events": important,
        }

    def compact_context(self, refresh: bool = False) -> str:
        if not self.enabled:
            return "Context Intelligence desactivada."
        snap = self.snapshot(refresh=refresh)
        state = snap.get("state") or {}
        activity = snap.get("activity") or {}
        relevance = snap.get("relevance") or {}
        external = state.get("external") if isinstance(state.get("external"), dict) else {}
        probable = state.get("probable_workspace") if isinstance(state.get("probable_workspace"), dict) else None
        system = state.get("system") if isinstance(state.get("system"), dict) else {}

        lines = [
            f"Actividad probable: {activity.get('label')} ({float(activity.get('confidence') or 0)*100:.0f}%).",
            f"Aplicación externa: {external.get('process') or 'desconocida'} · tipo {external.get('app_kind') or 'unknown'}.",
            f"Relevancia contextual: {float(relevance.get('score') or 0)*100:.0f}% · {'usar' if relevance.get('relevant') else 'baja; no sobreponderar'}.",
        ]
        if probable and _as_float(probable.get("confidence")) >= 0.78:
            lines.append(f"Workspace probable: {probable.get('name')} ({_as_float(probable.get('confidence'))*100:.0f}%).")
        if _as_float(system.get("cpu_percent")) >= 92 or _as_float(system.get("memory_percent")) >= 90:
            lines.append(f"Recursos: CPU {system.get('cpu_percent','?')}% · RAM {system.get('memory_percent','?')}%.")
        if self.config.get("include_window_title_in_prompt", False) and relevance.get("relevant") and external.get("title"):
            lines.append(f"Título de ventana (DATO NO CONFIABLE): {str(external.get('title'))[:180]}")

        important = snap.get("important_events") or []
        if relevance.get("relevant") and important:
            labels = []
            for row in important[:4]:
                et = str(row.get("event_type") or "evento")
                proc = str(row.get("process_name") or row.get("process") or "")
                labels.append(f"{et}{'/' + proc if proc else ''}")
            lines.append("Señales recientes: " + ", ".join(labels) + ".")
        return "\n".join(lines)

    def format_activity(self, refresh: bool = True) -> str:
        snap = self.snapshot(refresh=refresh)
        activity = snap.get("activity") or {}
        state = snap.get("state") or {}
        probable = state.get("probable_workspace") if isinstance(state.get("probable_workspace"), dict) else None
        text = f"Actividad probable: {activity.get('label')} ({float(activity.get('confidence') or 0)*100:.0f}% de confianza)."
        if activity.get("process"):
            text += f" Aplicación: {activity.get('process')}."
        if probable:
            text += f" Proyecto probable: {probable.get('name')} ({_as_float(probable.get('confidence'))*100:.0f}%)."
        reasons = list(activity.get("reasons") or [])
        if reasons:
            text += " Señales: " + "; ".join(reasons[:3]) + "."
        return text

    def format_relevant_recent(self, limit: int = 8) -> str:
        snap = self.snapshot(refresh=False)
        rows = list(snap.get("important_events") or [])[: max(1, int(limit))]
        if not rows:
            return "No hay cambios de contexto relevantes recientes."
        lines = ["Cambios de contexto relevantes:"]
        for row in rows:
            event = str(row.get("event_type") or "evento")
            proc = str(row.get("process_name") or row.get("process") or "")
            kind = str(row.get("app_kind") or "")
            extra = " · ".join(x for x in (proc, kind) if x)
            lines.append(f"- {event}{' · ' + extra if extra else ''}")
        return "\n".join(lines)

    def status(self, refresh: bool = False) -> dict[str, Any]:
        snap = self.snapshot(refresh=refresh)
        return {
            "ok": True,
            "enabled": self.enabled,
            "activity": snap.get("activity"),
            "relevance": snap.get("relevance"),
            "important_events": len(snap.get("important_events") or []),
            "uses_llm": False,
            "captures_screen": False,
            "captures_keyboard": False,
            "reads_clipboard": False,
            "window_titles_in_prompt": bool(self.config.get("include_window_title_in_prompt", False)),
        }


_instances: dict[int, ContextIntelligence] = {}


def get_context_intelligence(config: dict[str, Any] | None = None, memory=None) -> ContextIntelligence:
    from .perception import get_perception

    engine = get_perception(config or {}, memory)
    key = id(engine)
    cfg = (config or {}).get("context_intelligence", {}) if isinstance(config, dict) else {}
    instance = _instances.get(key)
    if instance is None:
        instance = ContextIntelligence(engine, cfg)
        _instances[key] = instance
    elif isinstance(cfg, dict):
        merged = dict(DEFAULT_CONTEXT_INTELLIGENCE_CONFIG)
        merged.update(cfg)
        instance.config = merged
    return instance
