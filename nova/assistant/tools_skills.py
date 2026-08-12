from __future__ import annotations

from typing import Any

from .skills import get_skill_registry


def _fn(name: str, description: str, properties=None, required=None) -> dict[str, Any]:
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


def schemas_skills() -> list[dict[str, Any]]:
    param_spec = {
        "type": "object",
        "description": "Mapa nombre -> {type, required, description, default opcional}.",
        "additionalProperties": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["string", "integer", "number", "boolean"]},
                "required": {"type": "boolean"},
                "description": {"type": "string"},
                "default": {},
            },
        },
    }
    step_spec = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "instruction": {"type": "string"},
                "tool_hint": {"type": "string"},
                "verify": {"type": "string"},
                "optional": {"type": "boolean"},
            },
            "required": ["instruction"],
        },
    }
    return [
        _fn("skill_status", "Estado local del Skills Engine."),
        _fn("skill_list", "Lista habilidades disponibles para el workspace actual y globales.", {
            "include_disabled": {"type": "boolean"}, "limit": {"type": "integer"}
        }),
        _fn("skill_search", "Busca habilidades relevantes por nombre, trigger o descripción.", {
            "query": {"type": "string"}, "limit": {"type": "integer"}
        }, ["query"]),
        _fn("skill_info", "Muestra la definición declarativa de una habilidad.", {
            "skill": {"type": "string"}
        }, ["skill"]),
        _fn(
            "skill_save",
            "Guarda o actualiza una habilidad declarativa. No admite código ejecutable ni secretos; los permisos declarados nunca sustituyen la política de seguridad.",
            {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "triggers": {"type": "array", "items": {"type": "string"}},
                "parameters": param_spec,
                "steps": step_spec,
                "verification": {"type": "array", "items": {"type": "string"}},
                "permissions": {"type": "array", "items": {"type": "string"}},
                "workspace": {"type": "boolean", "description": "Si true, limita la skill al workspace activo."},
                "source": {"type": "string", "description": "Usa 'user' si el usuario pidió explícitamente guardar la habilidad; en otro caso 'nova'."},
            },
            ["name", "steps"],
        ),
        _fn(
            "skill_run",
            "Prepara una ejecución de una habilidad y devuelve el playbook enlazado a sus parámetros. El Agent debe seguir ese playbook usando herramientas normales y respetando confirmaciones.",
            {
                "skill": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": {}},
            },
            ["skill"],
        ),
        _fn(
            "skill_finish",
            "Marca una ejecución preparada como verificada correctamente, fallida o simplemente finalizada por el Agent.",
            {
                "run_id": {"type": "integer"},
                "success": {"type": ["boolean", "null"]},
                "summary": {"type": "string"},
            },
            ["run_id"],
        ),
        _fn("skill_set_enabled", "Habilita o deshabilita una habilidad sin borrarla.", {
            "skill": {"type": "string"}, "enabled": {"type": "boolean"}
        }, ["skill", "enabled"]),
        _fn("skill_runs", "Muestra ejecuciones recientes de habilidades.", {
            "limit": {"type": "integer"}
        }),
    ]


def install_tools_skills():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_skills():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_skills_patched", False):
        original_init = LocalTools.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.skills = get_skill_registry(self.config, getattr(self, "memory", None))

        def skill_status(self):
            return {"ok": True, **self.skills.status()}

        def skill_list(self, include_disabled=False, limit=100):
            rows = self.skills.list(include_disabled=bool(include_disabled), limit=int(limit or 100))
            return {"ok": True, "skills": rows, "count": len(rows)}

        def skill_search(self, query, limit=8):
            rows = self.skills.match(str(query or ""), limit=int(limit or 8))
            return {"ok": True, "query": query, "skills": rows}

        def skill_info(self, skill):
            row = self.skills.get(str(skill))
            if not row:
                return {"ok": False, "error": f"No encontré la habilidad: {skill}"}
            return {"ok": True, "skill": row, "revisions": self.skills.revisions(int(row["id"]), limit=8)}

        def skill_save(
            self, name, description="", triggers=None, parameters=None, steps=None,
            verification=None, permissions=None, workspace=False, source="nova",
        ):
            wid = None
            if bool(workspace):
                active = self.memory.active_workspace()
                if not active:
                    return {"ok": False, "error": "No hay workspace activo para guardar una habilidad de proyecto."}
                wid = int(active["id"])
            try:
                row = self.skills.save(
                    name=str(name), description=str(description or ""), triggers=list(triggers or []),
                    parameters=dict(parameters or {}), steps=list(steps or []), verification=list(verification or []),
                    permissions=list(permissions or []), workspace_id=wid, source=str(source or "nova"),
                    provenance={"saved_via": "LocalTools.skill_save"},
                )
                return {"ok": True, "skill": row}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def skill_run(self, skill, arguments=None):
            try:
                compiled = self.skills.compile(str(skill), dict(arguments or {}))
                if compiled.missing:
                    return {
                        "ok": False,
                        "error": "missing_parameters",
                        "missing": compiled.missing,
                        "skill": compiled.skill.get("name"),
                    }
                run_id = self.skills.start_run(compiled)
                return {
                    "ok": True,
                    "run_id": run_id,
                    "skill": compiled.skill.get("name"),
                    "version": compiled.skill.get("version"),
                    "playbook": self.skills.format_playbook(compiled, run_id=run_id),
                    "security": "La Skill no concede permisos; usa las confirmaciones y herramientas normales de Nova.",
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def skill_finish(self, run_id, success=None, summary=""):
            try:
                row = self.skills.finish_run(int(run_id), success, str(summary or ""))
                return {"ok": bool(row), "run": row, "error": None if row else "No encontré esa ejecución."}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def skill_set_enabled(self, skill, enabled):
            row = self.skills.set_enabled(str(skill), bool(enabled))
            return {"ok": bool(row), "skill": row, "error": None if row else f"No encontré la habilidad: {skill}"}

        def skill_runs(self, limit=20):
            return {"ok": True, "runs": self.skills.recent_runs(int(limit or 20))}

        LocalTools.__init__ = init
        LocalTools.skill_status = skill_status
        LocalTools.skill_list = skill_list
        LocalTools.skill_search = skill_search
        LocalTools.skill_info = skill_info
        LocalTools.skill_save = skill_save
        LocalTools.skill_run = skill_run
        LocalTools.skill_finish = skill_finish
        LocalTools.skill_set_enabled = skill_set_enabled
        LocalTools.skill_runs = skill_runs
        LocalTools._nova_skills_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_skills", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        always = {"skill_search", "skill_run", "skill_finish"}
        management = {
            "skill_status", "skill_list", "skill_info", "skill_save", "skill_set_enabled", "skill_runs"
        }
        cues = (
            "skill", "habilidad", "habilidades", "procedimiento", "rutina", "playbook",
            "guarda esto", "recuerda este proceso", "automatiza esto", "convierte esto",
            "qué sabes hacer", "que sabes hacer",
        )

        def selector(text):
            rows = list(original_selector(text))
            names = {x.get("function", {}).get("name") for x in rows}
            wanted = set(always)
            if any(cue in str(text or "").casefold() for cue in cues):
                wanted |= management
            rows += [by_name[name] for name in wanted if name in by_name and name not in names]
            return rows

        selector._nova_skills = True
        mod.select_tool_schemas = selector

    return mod
