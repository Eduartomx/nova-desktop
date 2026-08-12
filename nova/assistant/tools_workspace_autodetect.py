from __future__ import annotations

from typing import Any

from .workspace_autodetect import get_workspace_autodetector


def workspace_autodetect_schemas() -> list[dict[str, Any]]:
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
            "workspace_autodetect_status",
            "Muestra el estado del aprendizaje local app↔workspace, la sugerencia actual y si la activación automática está habilitada.",
            {"refresh": {"type": "boolean"}},
        ),
        fn(
            "workspace_autodetect_associations",
            "Lista asociaciones locales aprendidas entre aplicaciones y workspaces, sin títulos de ventana ni rutas observadas.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
        fn(
            "workspace_autodetect_learn_current",
            "Fija explícitamente la aplicación externa actual al workspace activo o indicado. Úsalo solo cuando el usuario pida que Nova aprenda esa asociación.",
            {"workspace": {"type": "string"}},
        ),
        fn(
            "workspace_autodetect_forget_current",
            "Olvida asociaciones aprendidas para la aplicación externa actual, opcionalmente solo para un workspace.",
            {"workspace": {"type": "string"}},
        ),
    ]


def install_tools_workspace_autodetect():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in workspace_autodetect_schemas():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_workspace_autodetect_patched", False):
        def workspace_autodetect_status(self, refresh=False):
            detector = get_workspace_autodetector(self.config, self.memory)
            return detector.status(refresh=bool(refresh))

        def workspace_autodetect_associations(self, limit=30):
            detector = get_workspace_autodetector(self.config, self.memory)
            return {"ok": True, "associations": detector.associations(limit=int(limit or 30))}

        def workspace_autodetect_learn_current(self, workspace=None):
            detector = get_workspace_autodetector(self.config, self.memory)
            wid = None
            if workspace not in (None, ""):
                ws = self.memory.resolve_workspace(workspace)
                if not ws:
                    return {"ok": False, "error": f"No encontré el workspace: {workspace}"}
                wid = int(ws["id"])
            return detector.pin_current_to_workspace(wid)

        def workspace_autodetect_forget_current(self, workspace=None):
            detector = get_workspace_autodetector(self.config, self.memory)
            state = detector.engine.current(refresh=True)
            external = state.get("external") if isinstance(state.get("external"), dict) else {}
            if not external:
                return {"ok": False, "error": "No hay una aplicación externa observada."}
            wid = None
            if workspace not in (None, ""):
                ws = self.memory.resolve_workspace(workspace)
                if not ws:
                    return {"ok": False, "error": f"No encontré el workspace: {workspace}"}
                wid = int(ws["id"])
            return detector.forget(
                process_name=str(external.get("process") or ""),
                app_kind=str(external.get("app_kind") or ""),
                workspace_id=wid,
            )

        LocalTools.workspace_autodetect_status = workspace_autodetect_status
        LocalTools.workspace_autodetect_associations = workspace_autodetect_associations
        LocalTools.workspace_autodetect_learn_current = workspace_autodetect_learn_current
        LocalTools.workspace_autodetect_forget_current = workspace_autodetect_forget_current
        LocalTools._nova_workspace_autodetect_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_workspace_autodetect", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {
            "workspace_autodetect_status",
            "workspace_autodetect_associations",
            "workspace_autodetect_learn_current",
            "workspace_autodetect_forget_current",
        }
        cues = (
            "que proyecto crees", "qué proyecto crees", "workspace automatico", "workspace automático",
            "auto detection", "autodeteccion", "autodetección", "asociacion de aplicacion", "asociación de aplicación",
            "aprende que esta aplicacion", "aprende que esta aplicación", "recuerda que esta aplicacion",
            "olvida esta asociacion", "olvida esta asociación",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(cue in (text or "").casefold() for cue in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_workspace_autodetect = True
        mod.select_tool_schemas = selector

    return mod
