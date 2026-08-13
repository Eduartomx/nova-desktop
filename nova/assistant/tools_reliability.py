from __future__ import annotations

from typing import Any

from .experience_reliability import get_skill_reliability


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


def schemas_reliability() -> list[dict[str, Any]]:
    return [
        _fn("skill_reliability_status", "Estado del Experience & Reliability Loop de Skills."),
        _fn(
            "skill_reliability_report",
            "Muestra la fiabilidad histórica de una Skill concreta.",
            {"skill": {"type": "string"}},
            ["skill"],
        ),
        _fn(
            "skill_reliability_review_queue",
            "Lista Skills degradadas u obsoletas que requieren revisión antes de reutilizarse.",
            {"limit": {"type": "integer"}},
        ),
    ]


def install_tools_reliability():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_reliability():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_reliability_patched", False):
        original_init = LocalTools.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            registry = getattr(self, "skills", None)
            self.skill_reliability = get_skill_reliability(self.config, registry)

        def skill_reliability_status(self):
            return {"ok": True, **self.skill_reliability.status()}

        def skill_reliability_report(self, skill):
            row = self.skill_reliability.report(str(skill))
            if not row:
                return {"ok": False, "error": f"No encontré la habilidad: {skill}"}
            return {"ok": True, "reliability": row}

        def skill_reliability_review_queue(self, limit=20):
            rows = self.skill_reliability.review_queue(int(limit or 20))
            return {"ok": True, "skills": rows, "count": len(rows)}

        LocalTools.__init__ = init
        LocalTools.skill_reliability_status = skill_reliability_status
        LocalTools.skill_reliability_report = skill_reliability_report
        LocalTools.skill_reliability_review_queue = skill_reliability_review_queue
        LocalTools._nova_reliability_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_reliability", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        wanted = {"skill_reliability_status", "skill_reliability_report", "skill_reliability_review_queue"}
        cues = (
            "fiabilidad", "reliability", "skill falla", "habilidad falla", "habilidades fallando",
            "skill obsoleta", "habilidad obsoleta", "skills obsoletas", "revisar skill",
            "revisar habilidad", "degradada", "degradadas",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x.get("function", {}).get("name") for x in rows}
            if any(cue in str(text or "").casefold() for cue in cues):
                rows += [by_name[name] for name in wanted if name in by_name and name not in present]
            return rows

        selector._nova_reliability = True
        mod.select_tool_schemas = selector

    return mod
