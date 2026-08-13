from __future__ import annotations

from typing import Any

from .learn_from_expert import get_expert_learning


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


def schemas_learning() -> list[dict[str, Any]]:
    return [
        _fn("expert_learning_status", "Estado de Learn from Expert y candidata actual."),
        _fn("expert_learning_candidate", "Muestra metadatos de la solución experta candidata; no devuelve contenido externo crudo."),
        _fn(
            "expert_learning_verify",
            "Registra el resultado de una comprobación local de la solución experta. Llamar solo después de verificar realmente el resultado.",
            {
                "success": {"type": "boolean"},
                "source": {"type": "string", "enum": ["tool", "skill", "user", "manual_check"]},
                "note": {"type": "string"},
            },
            ["success"],
        ),
        _fn(
            "expert_learning_save_skill",
            "Convierte una solución experta YA VERIFICADA en una Skill declarativa draft. No guarda la respuesta externa cruda.",
            {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "triggers": {"type": "array", "items": {"type": "string"}},
                "steps": {
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
                },
                "verification": {"type": "array", "items": {"type": "string"}},
                "workspace": {"type": "boolean"},
                "memory_summary": {"type": "string"},
            },
            ["name", "steps"],
        ),
        _fn("expert_learning_discard", "Descarta la candidata actual sin aprenderla."),
        _fn("expert_learning_recent", "Muestra historial técnico de aprendizaje experto, sin contenido externo.", {"limit": {"type": "integer"}}),
    ]


def install_tools_learning():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_learning():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_learning_patched", False):
        original_init = LocalTools.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.expert_learning = get_expert_learning(self.config, getattr(self, "memory", None))

        def expert_learning_status(self):
            return {"ok": True, **self.expert_learning.status()}

        def expert_learning_candidate(self):
            return {"ok": True, "candidate": self.expert_learning.candidate()}

        def expert_learning_verify(self, success, source="tool", note=""):
            return self.expert_learning.verify(bool(success), str(source or "tool"), str(note or ""))

        def expert_learning_save_skill(self, name, steps, description="", triggers=None, verification=None,
                                       workspace=True, memory_summary=""):
            return self.expert_learning.save_skill(
                name=str(name or ""),
                description=str(description or ""),
                triggers=list(triggers or []),
                steps=list(steps or []),
                verification=list(verification or []),
                workspace=bool(workspace),
                memory_summary=str(memory_summary or ""),
            )

        def expert_learning_discard(self):
            return self.expert_learning.discard()

        def expert_learning_recent(self, limit=20):
            return {"ok": True, "events": self.expert_learning.recent(int(limit or 20))}

        LocalTools.__init__ = init
        LocalTools.expert_learning_status = expert_learning_status
        LocalTools.expert_learning_candidate = expert_learning_candidate
        LocalTools.expert_learning_verify = expert_learning_verify
        LocalTools.expert_learning_save_skill = expert_learning_save_skill
        LocalTools.expert_learning_discard = expert_learning_discard
        LocalTools.expert_learning_recent = expert_learning_recent
        LocalTools._nova_learning_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_learning", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {
            "expert_learning_status", "expert_learning_candidate", "expert_learning_verify",
            "expert_learning_save_skill", "expert_learning_discard", "expert_learning_recent",
        }
        cues = (
            "aprende esta solucion", "aprende esta solución", "guarda lo aprendido", "aprendizaje experto",
            "learn from expert", "esto funciono", "esto funcionó", "la solucion funciono", "la solución funcionó",
            "verifica la solucion", "verifica la solución", "descarta esta solucion", "descarta esta solución",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x.get("function", {}).get("name") for x in rows}
            if any(cue in str(text or "").casefold() for cue in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_learning = True
        mod.select_tool_schemas = selector

    return mod
