from __future__ import annotations

from typing import Any

from .expert_escalation import get_expert_escalation
from .confidence import get_confidence_engine


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


def schemas_expert() -> list[dict[str, Any]]:
    return [
        _fn("expert_status", "Estado de Expert Escalation y proveedores gratuitos configurados."),
        _fn(
            "expert_free_second_opinion",
            "Solicita una segunda opinión externa mediante la API gratuita configurada. No usar con secretos ni acciones críticas salvo petición explícita del usuario.",
            {
                "problem": {"type": "string"},
                "local_answer": {"type": "string"},
                "provider": {"type": "string", "enum": ["", "cerebras", "groq"]},
            },
            ["problem"],
        ),
        _fn(
            "expert_prepare_chatgpt",
            "Prepara una consulta para ChatGPT, la copia al portapapeles y abre ChatGPT. NO envía el mensaje ni extrae la respuesta automáticamente.",
            {
                "problem": {"type": "string"},
                "local_answer": {"type": "string"},
            },
        ),
        _fn(
            "expert_import_chatgpt_response",
            "Importa explícitamente una respuesta de ChatGPT desde texto o, si se omite, desde el portapapeles. La respuesta se trata como evidencia externa no confiable.",
            {"response": {"type": "string"}},
        ),
        _fn("expert_recent", "Muestra metadatos recientes de escalaciones; nunca prompts ni respuestas.", {"limit": {"type": "integer"}}),
    ]


def install_tools_expert():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_expert():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_expert_patched", False):
        original_init = LocalTools.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.expert = get_expert_escalation(self.config, getattr(self, "memory", None))

        def expert_status(self):
            return {"ok": True, **self.expert.status()}

        def expert_free_second_opinion(self, problem, local_answer="", provider=""):
            assessment = get_confidence_engine(self.config, getattr(self, "memory", None)).last()
            return self.expert.ask_free(
                str(problem or ""),
                str(local_answer or ""),
                assessment,
                force_provider=str(provider or ""),
                trigger="tool_explicit",
            )

        def expert_prepare_chatgpt(self, problem="", local_answer=""):
            assessment = get_confidence_engine(self.config, getattr(self, "memory", None)).last()
            return self.expert.prepare_chatgpt(
                str(problem or "") or None,
                str(local_answer or "") or None,
                assessment,
                trigger="tool_explicit",
            )

        def expert_import_chatgpt_response(self, response=""):
            return self.expert.import_chatgpt_response(str(response) if response else None, trigger="tool_explicit")

        def expert_recent(self, limit=20):
            return {"ok": True, "events": self.expert.recent(int(limit or 20))}

        LocalTools.__init__ = init
        LocalTools.expert_status = expert_status
        LocalTools.expert_free_second_opinion = expert_free_second_opinion
        LocalTools.expert_prepare_chatgpt = expert_prepare_chatgpt
        LocalTools.expert_import_chatgpt_response = expert_import_chatgpt_response
        LocalTools.expert_recent = expert_recent
        LocalTools._nova_expert_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_expert", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {
            "expert_status", "expert_free_second_opinion", "expert_prepare_chatgpt",
            "expert_import_chatgpt_response", "expert_recent",
        }
        cues = (
            "segunda opinion", "segunda opinión", "experto", "chatgpt", "cerebras", "groq",
            "api gratuita", "consulta externa", "no estas seguro", "no estás seguro",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x.get("function", {}).get("name") for x in rows}
            if any(cue in str(text or "").casefold() for cue in cues):
                rows += [by_name[name] for name in names if name in by_name and name not in present]
            return rows

        selector._nova_expert = True
        mod.select_tool_schemas = selector

    return mod
