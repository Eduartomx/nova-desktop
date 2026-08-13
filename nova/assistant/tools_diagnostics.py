from __future__ import annotations

from typing import Any

from .llm_performance import get_llm_performance
from .profiler import get_profiler
from .self_repair import SelfRepairManager


def schemas_v066() -> list[dict[str, Any]]:
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
            "performance_summary",
            "Resume el rendimiento local de Nova sin enviar telemetría fuera del PC.",
            {
                "hours": {"type": "number", "minimum": 0.1, "maximum": 720},
                "session_only": {"type": "boolean"},
            },
        ),
        fn(
            "performance_recent",
            "Lista las operaciones recientes medidas por el profiler local de Nova.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
        fn(
            "llm_performance_summary",
            "Resume métricas locales de Ollama: carga, prompt eval, generación, tokens/s y presión puntual de GPU/VRAM. No contiene prompts ni respuestas.",
            {
                "hours": {"type": "number", "minimum": 0.1, "maximum": 720},
                "session_only": {"type": "boolean"},
            },
        ),
        fn(
            "doctor_repairs",
            "Ejecuta Nova Doctor y devuelve qué reparaciones deterministas están disponibles. No instala ni modifica nada.",
        ),
    ]


def install_tools_v066():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in schemas_v066():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_v066_patched", False):
        def performance_summary(self, hours=24, session_only=False):
            profiler = get_profiler(self.config)
            report = profiler.summary(float(hours or 24), session_only=bool(session_only))
            return {"ok": True, "report": report, "text": profiler.format_summary(report)}

        def performance_recent(self, limit=20):
            profiler = get_profiler(self.config)
            return {"ok": True, "events": profiler.recent(int(limit or 20))}

        def llm_performance_summary(self, hours=24, session_only=False):
            monitor = get_llm_performance(self.config)
            report = monitor.summary(float(hours or 24), session_only=bool(session_only))
            return {"ok": True, "report": report, "text": monitor.format_summary(report)}

        def doctor_repairs(self):
            from .doctor import NovaDoctor
            report = NovaDoctor(self.config, self.memory).run()
            manager = SelfRepairManager(self.config, self.memory)
            actions = manager.available_actions(report)
            return {"ok": True, "severity": report.get("severity"), "repairs": actions}

        LocalTools.performance_summary = performance_summary
        LocalTools.performance_recent = performance_recent
        LocalTools.llm_performance_summary = llm_performance_summary
        LocalTools.doctor_repairs = doctor_repairs
        LocalTools._nova_v066_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_v066", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {"performance_summary", "performance_recent", "llm_performance_summary", "doctor_repairs"}
        cues = (
            "rendimiento", "performance", "profiler", "perfil de rendimiento",
            "lento", "lentitud", "cuello de botella", "cuellos de botella",
            "ollama", "qwen", "llm", "tokens por segundo", "tok/s", "cold start",
            "doctor", "reparar nova", "que puedes reparar", "qué puedes reparar",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(cue in (text or "").casefold() for cue in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_v066 = True
        mod.select_tool_schemas = selector

    return mod
