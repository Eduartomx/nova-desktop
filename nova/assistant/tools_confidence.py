from __future__ import annotations

import functools
from typing import Any

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


def schemas_confidence() -> list[dict[str, Any]]:
    return [
        _fn("confidence_status", "Estado del Confidence Engine. El score es heurístico y no una probabilidad calibrada."),
        _fn("confidence_last", "Devuelve la última evaluación de confianza basada en evidencia estructurada."),
        _fn("confidence_recent", "Lista evaluaciones recientes sin prompts ni respuestas.", {
            "limit": {"type": "integer"}
        }),
        _fn(
            "confidence_assess",
            "Evalúa manualmente señales estructuradas. No acepta texto libre ni confianza autodeclarada por el LLM.",
            {
                "request_kind": {"type": "string", "enum": ["simple_control", "creative", "current_state", "factual", "planning", "diagnosis"]},
                "risk_level": {"type": "string", "enum": ["normal", "high", "critical"]},
                "structured_reads": {"type": "integer"},
                "verifications": {"type": "integer"},
                "failures": {"type": "integer"},
                "contradictions": {"type": "integer"},
                "deterministic": {"type": "boolean"},
                "skill_trust": {"type": "string", "enum": ["", "draft", "user", "verified"]},
            },
        ),
    ]


def install_tools_confidence():
    from . import tools as mod

    # Capturamos las herramientas preexistentes antes de añadir las del propio
    # Confidence Engine. Así no se auto-mide a sí mismo ni crea evidencia circular.
    instrument_names = []
    for schema in mod.TOOL_SCHEMAS:
        name = schema.get("function", {}).get("name")
        if name:
            instrument_names.append(str(name))

    existing = set(instrument_names)
    for schema in schemas_confidence():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_confidence_patched", False):
        original_init = LocalTools.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.confidence = get_confidence_engine(self.config, getattr(self, "memory", None))

        LocalTools.__init__ = init

        def confidence_status(self):
            return {"ok": True, **self.confidence.status()}

        def confidence_last(self):
            row = self.confidence.last()
            return {"ok": bool(row), "assessment": row, "error": None if row else "Todavía no hay evaluaciones."}

        def confidence_recent(self, limit=20):
            rows = self.confidence.recent(int(limit or 20))
            return {"ok": True, "assessments": rows, "count": len(rows)}

        def confidence_assess(
            self, request_kind="factual", risk_level="normal", structured_reads=0,
            verifications=0, failures=0, contradictions=0, deterministic=False, skill_trust="",
        ):
            result = self.confidence.manual_assess(
                request_kind=str(request_kind or "factual"), risk_level=str(risk_level or "normal"),
                structured_reads=int(structured_reads or 0), verifications=int(verifications or 0),
                failures=int(failures or 0), contradictions=int(contradictions or 0),
                deterministic=bool(deterministic), skill_trust=str(skill_trust or ""),
            )
            return {"ok": True, "assessment": result}

        LocalTools.confidence_status = confidence_status
        LocalTools.confidence_last = confidence_last
        LocalTools.confidence_recent = confidence_recent
        LocalTools.confidence_assess = confidence_assess

        # Instrumentación transversal: cada herramienta existente aporta únicamente
        # metadatos de éxito/fallo y su nombre. Nunca se persisten argumentos/output.
        for name in instrument_names:
            if name.startswith("confidence_") or not hasattr(LocalTools, name):
                continue
            original = getattr(LocalTools, name)
            if not callable(original) or getattr(original, "_nova_confidence_wrapped", False):
                continue

            @functools.wraps(original)
            def wrapped(self, *args, __original=original, __name=name, **kwargs):
                engine = getattr(self, "confidence", None)
                try:
                    result = __original(self, *args, **kwargs)
                except Exception:
                    if engine is not None:
                        try:
                            engine.record_tool(__name, None, failed=True)
                        except Exception:
                            pass
                    raise
                if engine is not None:
                    try:
                        engine.record_tool(__name, result, failed=False)
                    except Exception:
                        pass
                return result

            wrapped._nova_confidence_wrapped = True
            setattr(LocalTools, name, wrapped)

        LocalTools._nova_confidence_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_confidence", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {"confidence_status", "confidence_last", "confidence_recent", "confidence_assess"}
        cues = (
            "confianza", "confidence", "qué tan seguro", "que tan seguro", "estás seguro", "estas seguro",
            "certeza", "evidencia", "segunda opinión", "segunda opinion", "no estás seguro", "no estas seguro",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x.get("function", {}).get("name") for x in rows}
            if any(cue in str(text or "").casefold() for cue in cues):
                rows += [by_name[name] for name in names if name in by_name and name not in present]
            return rows

        selector._nova_confidence = True
        mod.select_tool_schemas = selector

    return mod
