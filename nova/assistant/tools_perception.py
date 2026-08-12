from __future__ import annotations

from typing import Any

from .perception import get_perception


def perception_schemas() -> list[dict[str, Any]]:
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
            "perception_context",
            "Devuelve el contexto estructurado actual del escritorio observado por Perception Engine: aplicación/ventana externa, workspace probable y carga básica del sistema. No hace screenshot.",
            {"refresh": {"type": "boolean"}},
        ),
        fn(
            "perception_status",
            "Devuelve estado y garantías de privacidad de Perception Engine.",
            {"refresh": {"type": "boolean"}},
        ),
        fn(
            "perception_recent",
            "Lista cambios recientes de aplicación, workspace probable o presión del sistema detectados localmente.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
    ]


def install_tools_perception():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in perception_schemas():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_perception_patched", False):
        def perception_context(self, refresh=True):
            engine = get_perception(self.config, self.memory)
            state = engine.current(refresh=bool(refresh))
            return {"ok": True, "context": state, "text": engine.format_current(refresh=False)}

        def perception_status(self, refresh=False):
            engine = get_perception(self.config, self.memory)
            return engine.status(refresh=bool(refresh))

        def perception_recent(self, limit=20):
            engine = get_perception(self.config, self.memory)
            events = engine.recent_events(int(limit or 20))
            return {"ok": True, "events": events, "text": engine.format_recent(int(limit or 20))}

        LocalTools.perception_context = perception_context
        LocalTools.perception_status = perception_status
        LocalTools.perception_recent = perception_recent
        LocalTools._nova_perception_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_perception", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {"perception_context", "perception_status", "perception_recent"}
        cues = (
            "percepcion", "percepción", "contexto del escritorio", "ventana activa",
            "aplicacion abierta", "aplicación abierta", "que estaba usando", "qué estaba usando",
            "que ventana", "qué ventana", "workspace probable", "proyecto probable",
            "cambios de contexto", "aplicaciones recientes",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(cue in (text or "").casefold() for cue in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_perception = True
        mod.select_tool_schemas = selector

    return mod
