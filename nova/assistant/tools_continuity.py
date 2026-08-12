from __future__ import annotations

from typing import Any


def schemas_v065() -> list[dict[str, Any]]:
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
            "continuity_resume",
            "Recupera el último estado de trabajo del workspace: checkpoint, tareas abiertas y pendientes para poder continuar.",
            {"workspace": {"type": "string"}},
        ),
        fn(
            "continuity_pending",
            "Lista lo que quedó pendiente en el workspace actual o indicado.",
            {"workspace": {"type": "string"}},
        ),
        fn(
            "continuity_history",
            "Muestra checkpoints recientes del trabajo realizado en un workspace.",
            {"workspace": {"type": "string"}, "limit": {"type": "integer"}},
        ),
        fn(
            "continuity_checkpoint",
            "Guarda un checkpoint estructurado de la sesión actual: resumen, completado, pendiente, archivos, decisiones y errores.",
            {
                "summary": {"type": "string"},
                "completed": {"type": "array", "items": {"type": "string"}},
                "pending": {"type": "array", "items": {"type": "string"}},
                "files": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "errors": {"type": "array", "items": {"type": "string"}},
                "workspace": {"type": "string"},
                "status": {"type": "string"},
            },
            ["summary"],
        ),
        fn(
            "continuity_close",
            "Cierra la sesión de continuidad actual como completada, cancelada o abandonada.",
            {"workspace": {"type": "string"}, "status": {"type": "string"}, "summary": {"type": "string"}},
        ),
    ]


def _workspace_for(memory, selector=""):
    if selector:
        return memory.resolve_workspace(selector)
    return memory.active_workspace()


def _format_checkpoint(cp: dict[str, Any] | None) -> dict[str, Any] | None:
    if not cp:
        return None
    return {
        "id": cp.get("id"),
        "kind": cp.get("kind"),
        "summary": cp.get("summary"),
        "completed": cp.get("completed") or [],
        "pending": cp.get("pending") or [],
        "files": cp.get("files") or [],
        "decisions": cp.get("decisions") or [],
        "errors": cp.get("errors") or [],
        "created_at": cp.get("created_at"),
    }


def install_tools_v065():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_v065():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_v065_patched", False):

        def continuity_resume(self, workspace=""):
            ws = _workspace_for(self.memory, workspace)
            if workspace and not ws:
                return {"ok": False, "error": f"No encontré el workspace: {workspace}"}
            wid = int(ws["id"]) if ws else None
            state = self.memory.continuity_resume(workspace_id=wid, any_if_none=ws is None and not workspace)
            state["workspace"] = ws.get("name") if ws else None
            if state.get("session") and state["session"].get("workspace_id") and ws is None:
                try:
                    resolved = self.memory.get_workspace(int(state["session"]["workspace_id"]))
                    state["workspace"] = resolved.get("name") if resolved else None
                except Exception:
                    pass
            state["checkpoint"] = _format_checkpoint(state.get("checkpoint"))
            return state

        def continuity_pending(self, workspace=""):
            ws = _workspace_for(self.memory, workspace)
            if workspace and not ws:
                return {"ok": False, "error": f"No encontré el workspace: {workspace}"}
            wid = int(ws["id"]) if ws else None
            state = self.memory.continuity_pending(workspace_id=wid, any_if_none=ws is None and not workspace)
            state["workspace"] = ws.get("name") if ws else None
            state["checkpoint"] = _format_checkpoint(state.get("checkpoint"))
            return state

        def continuity_history(self, workspace="", limit=12):
            ws = _workspace_for(self.memory, workspace)
            if workspace and not ws:
                return {"ok": False, "error": f"No encontré el workspace: {workspace}"}
            wid = int(ws["id"]) if ws else None
            rows = self.memory.continuity_history(
                workspace_id=wid,
                limit=max(1, min(int(limit or 12), 50)),
                any_if_none=ws is None and not workspace,
            )
            return {
                "ok": bool(rows),
                "workspace": ws.get("name") if ws else None,
                "history": [_format_checkpoint(row) for row in rows],
            }

        def continuity_checkpoint(self, summary, completed=None, pending=None, files=None, decisions=None,
                                  errors=None, workspace="", status="active"):
            ws = _workspace_for(self.memory, workspace)
            if workspace and not ws:
                return {"ok": False, "error": f"No encontré el workspace: {workspace}"}
            wid = int(ws["id"]) if ws else None
            result = self.memory.continuity_checkpoint(
                workspace_id=wid,
                summary=str(summary or ""),
                completed=completed or [],
                pending=pending or [],
                files=files or [],
                decisions=decisions or [],
                errors=errors or [],
                metadata={"source": "tool"},
                kind="manual",
                session_status=str(status or "active").casefold(),
            )
            result["workspace"] = ws.get("name") if ws else None
            result["checkpoint"] = _format_checkpoint(result.get("checkpoint"))
            return result

        def continuity_close(self, workspace="", status="completed", summary=""):
            ws = _workspace_for(self.memory, workspace)
            if workspace and not ws:
                return {"ok": False, "error": f"No encontré el workspace: {workspace}"}
            wid = int(ws["id"]) if ws else None
            result = self.memory.continuity_close(
                workspace_id=wid,
                status=str(status or "completed").casefold(),
                summary=str(summary or ""),
            )
            result["workspace"] = ws.get("name") if ws else None
            return result

        LocalTools.continuity_resume = continuity_resume
        LocalTools.continuity_pending = continuity_pending
        LocalTools.continuity_history = continuity_history
        LocalTools.continuity_checkpoint = continuity_checkpoint
        LocalTools.continuity_close = continuity_close
        LocalTools._nova_v065_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_v065", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {
            "continuity_resume", "continuity_pending", "continuity_history",
            "continuity_checkpoint", "continuity_close",
        }
        cues = (
            "continúa", "continua", "continuar", "retoma", "retomar", "dónde nos quedamos", "donde nos quedamos",
            "quedó pendiente", "quedo pendiente", "pendiente del proyecto", "qué falta del proyecto", "que falta del proyecto",
            "qué hicimos ayer", "que hicimos ayer", "qué hemos hecho", "que hemos hecho", "historial del proyecto",
            "checkpoint", "punto de control", "guarda el estado", "cierra la sesión", "terminamos este proyecto",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(cue in (text or "").casefold() for cue in cues):
                rows += [by_name[name] for name in names if name in by_name and name not in present]
            return rows

        selector._nova_v065 = True
        mod.select_tool_schemas = selector

    return mod
