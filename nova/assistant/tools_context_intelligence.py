from __future__ import annotations

from typing import Any

from .context_intelligence import get_context_intelligence


def context_intelligence_schemas() -> list[dict[str, Any]]:
    def fn(name, description, properties=None, required=None):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties or {},
                    "required": required or [],
                },
            },
        }

    return [
        fn(
            "context_activity",
            "Infiere localmente la actividad probable del usuario a partir de Perception Engine, por ejemplo programando, navegando o jugando. No usa LLM ni screenshot.",
            {"refresh": {"type": "boolean"}},
        ),
        fn(
            "context_relevant_recent",
            "Resume solo los cambios de contexto recientes con mayor relevancia, reduciendo rebotes repetitivos entre ventanas.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        ),
        fn(
            "context_intelligence_status",
            "Devuelve la puntuación de relevancia contextual, actividad inferida y garantías de privacidad de Context Intelligence.",
            {"refresh": {"type": "boolean"}},
        ),
    ]


def install_tools_context_intelligence():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in context_intelligence_schemas():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_context_intelligence_patched", False):
        def context_activity(self, refresh=True):
            intelligence = get_context_intelligence(self.config, self.memory)
            return {
                "ok": True,
                "snapshot": intelligence.snapshot(refresh=bool(refresh)),
                "text": intelligence.format_activity(refresh=False),
            }

        def context_relevant_recent(self, limit=8):
            intelligence = get_context_intelligence(self.config, self.memory)
            return {
                "ok": True,
                "snapshot": intelligence.snapshot(refresh=False),
                "text": intelligence.format_relevant_recent(int(limit or 8)),
            }

        def context_intelligence_status(self, refresh=False):
            intelligence = get_context_intelligence(self.config, self.memory)
            return intelligence.status(refresh=bool(refresh))

        LocalTools.context_activity = context_activity
        LocalTools.context_relevant_recent = context_relevant_recent
        LocalTools.context_intelligence_status = context_intelligence_status
        LocalTools._nova_context_intelligence_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_context_intelligence", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {"context_activity", "context_relevant_recent", "context_intelligence_status"}
        cues = (
            "que estoy haciendo", "qué estoy haciendo", "actividad probable", "actividad actual",
            "context intelligence", "inteligencia de contexto", "contexto relevante",
            "cambios importantes de contexto", "cambios relevantes", "ruido de contexto",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(cue in (text or "").casefold() for cue in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_context_intelligence = True
        mod.select_tool_schemas = selector

    return mod
