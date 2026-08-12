from __future__ import annotations

from pathlib import Path
from typing import Any

from .workspace_index import WorkspaceIndexer


def schemas_v061() -> list[dict[str, Any]]:
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
            "workspace_index",
            "Indexa el workspace activo de forma incremental para detectar cambios y buscar archivos rápidamente.",
            {"workspace": {"type": "string"}, "force": {"type": "boolean"}},
        ),
        fn(
            "workspace_changes",
            "Muestra archivos añadidos, modificados o eliminados detectados en el último indexado del workspace.",
            {"workspace": {"type": "string"}, "limit": {"type": "integer"}, "refresh": {"type": "boolean"}},
        ),
        fn(
            "workspace_search",
            "Busca rutas de archivos dentro del índice local del workspace sin recorrer de nuevo todo el disco.",
            {"query": {"type": "string"}, "workspace": {"type": "string"}, "limit": {"type": "integer"}, "auto_index": {"type": "boolean"}},
            ["query"],
        ),
        fn(
            "workspace_index_status",
            "Obtiene el estado del índice local del workspace: archivos indexados y último análisis.",
            {"workspace": {"type": "string"}},
        ),
    ]


def install_tools_v061():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_v061():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_v061_patched", False):
        original_init = LocalTools.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.workspace_indexer = WorkspaceIndexer(self.memory, self.config.get("workspace", {}))

        def resolve(self, selector=""):
            ws = self.memory.resolve_workspace(selector or None)
            if not ws:
                return None, {"ok": False, "error": "No hay workspace activo o no encontré el indicado."}
            p = Path(str(ws.get("path", "")))
            if not p.is_dir():
                return None, {"ok": False, "error": f"La carpeta del workspace ya no existe: {p}"}
            return ws, None

        def workspace_index(self, workspace="", force=False):
            ws, err = resolve(self, workspace)
            if err:
                return err
            result = self.workspace_indexer.index(ws, force=bool(force))
            if result.get("ok"):
                result["changes"] = self.workspace_indexer.changes(int(ws["id"]), limit=40, run_id=result["run_id"])
            return result

        def workspace_changes(self, workspace="", limit=80, refresh=False):
            ws, err = resolve(self, workspace)
            if err:
                return err
            if bool(refresh):
                indexed = self.workspace_indexer.index(ws)
                if not indexed.get("ok"):
                    return indexed
                run_id = indexed["run_id"]
            else:
                status = self.workspace_indexer.status(int(ws["id"]))
                if not status.get("last_run"):
                    indexed = self.workspace_indexer.index(ws)
                    if not indexed.get("ok"):
                        return indexed
                    run_id = indexed["run_id"]
                else:
                    run_id = int(status["last_run"]["id"])
            rows = self.workspace_indexer.changes(int(ws["id"]), limit=limit, run_id=run_id)
            return {
                "ok": True,
                "workspace": ws.get("name"),
                "run_id": run_id,
                "changes": rows,
                "counts": {
                    "added": sum(1 for x in rows if x["change_type"] == "added"),
                    "modified": sum(1 for x in rows if x["change_type"] == "modified"),
                    "removed": sum(1 for x in rows if x["change_type"] == "removed"),
                },
            }

        def workspace_search(self, query, workspace="", limit=30, auto_index=True):
            ws, err = resolve(self, workspace)
            if err:
                return err
            status = self.workspace_indexer.status(int(ws["id"]))
            if bool(auto_index) and not status.get("last_run"):
                indexed = self.workspace_indexer.index(ws)
                if not indexed.get("ok"):
                    return indexed
                status = self.workspace_indexer.status(int(ws["id"]))
            rows = self.workspace_indexer.search(int(ws["id"]), query, limit)
            return {
                "ok": True,
                "workspace": ws.get("name"),
                "query": query,
                "indexed_files": status.get("indexed_files", 0),
                "results": rows,
            }

        def workspace_index_status(self, workspace=""):
            ws, err = resolve(self, workspace)
            if err:
                return err
            return {"ok": True, "workspace": ws.get("name"), "status": self.workspace_indexer.status(int(ws["id"]))}

        LocalTools.__init__ = init
        LocalTools.workspace_index = workspace_index
        LocalTools.workspace_changes = workspace_changes
        LocalTools.workspace_search = workspace_search
        LocalTools.workspace_index_status = workspace_index_status
        LocalTools._nova_v061_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_v061", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {"workspace_index", "workspace_changes", "workspace_search", "workspace_index_status"}
        cues = (
            "qué cambió", "que cambio", "qué cambio", "cambios", "modificado", "modificó",
            "archivo", "archivos", "buscar en el proyecto", "busca en el proyecto",
            "dónde está", "donde esta", "indexa", "indexar", "índice", "indice",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(c in (text or "").casefold() for c in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_v061 = True
        mod.select_tool_schemas = selector

    return mod
