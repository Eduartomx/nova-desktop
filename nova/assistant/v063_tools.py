from __future__ import annotations

from typing import Any


def schemas_v063() -> list[dict[str, Any]]:
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
            "memory_semantic_status",
            "Comprueba si Semantic Memory está activa, qué modelo usa y cuántos recuerdos tienen embeddings locales.",
            {"refresh": {"type": "boolean"}},
        ),
        fn(
            "memory_semantic_reindex",
            "Regenera embeddings locales de la memoria. No descarga modelos automáticamente.",
            {"force": {"type": "boolean"}, "workspace_only": {"type": "boolean"}},
        ),
    ]


def install_tools_v063():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_v063():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_v063_patched", False):
        def memory_semantic_status(self, refresh=False):
            active = self.memory.active_workspace()
            wid = int(active["id"]) if active else None
            status = self.memory.semantic_status(workspace_id=wid, refresh=bool(refresh))
            status["workspace"] = active.get("name") if active else None
            return {"ok": True, "status": status}

        def memory_semantic_reindex(self, force=False, workspace_only=False):
            active = self.memory.active_workspace() if workspace_only else None
            if workspace_only and not active:
                return {"ok": False, "error": "No hay workspace activo."}
            wid = int(active["id"]) if active else None
            result = self.memory.semantic_reindex(workspace_id=wid, force=bool(force))
            result["workspace"] = active.get("name") if active else None
            if not result.get("ok"):
                status = self.memory.semantic_status(workspace_id=wid)
                result["install_command"] = status.get("install_command")
            return result

        LocalTools.memory_semantic_status = memory_semantic_status
        LocalTools.memory_semantic_reindex = memory_semantic_reindex
        LocalTools._nova_v063_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_v063", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {"memory_search", "memory_semantic_status", "memory_semantic_reindex"}
        cues = (
            "memoria semántica", "memoria semantica", "embedding", "embeddings",
            "reindexa la memoria", "reindexar memoria", "problema parecido", "algo parecido",
            "recuerdas algo", "qué recuerdas", "que recuerdas", "busca en tu memoria",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(c in (text or "").casefold() for c in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_v063 = True
        mod.select_tool_schemas = selector

    return mod
