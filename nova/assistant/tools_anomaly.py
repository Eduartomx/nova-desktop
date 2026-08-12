from __future__ import annotations

from typing import Any

from .anomaly import get_anomaly_detector


def anomaly_schemas() -> list[dict[str, Any]]:
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
            "anomaly_status",
            "Devuelve el estado del detector local de anomalías, madurez de la línea base y anomalías pendientes. No usa LLM ni ejecuta reparaciones.",
            {"refresh": {"type": "boolean"}},
        ),
        fn(
            "anomaly_recent",
            "Lista anomalías recientes de CPU/RAM, procesos con consumo inesperado y señales de crash detectadas localmente.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
        fn(
            "anomaly_mark_process_expected",
            "Marca un nombre de proceso como esperado para evitar futuros avisos por consumo inusual. Solo usar si el usuario lo pide explícitamente.",
            {
                "process_name": {"type": "string"},
                "expected": {"type": "boolean"},
            },
            ["process_name"],
        ),
        fn(
            "anomaly_acknowledge",
            "Marca una anomalía concreta o todas las anomalías pendientes como revisadas.",
            {"event_id": {"type": "integer"}},
        ),
    ]


def install_tools_anomaly():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in anomaly_schemas():
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_anomaly_patched", False):
        def anomaly_status(self, refresh=False):
            detector = get_anomaly_detector(self.config, self.memory)
            return detector.status(refresh=bool(refresh))

        def anomaly_recent(self, limit=20):
            detector = get_anomaly_detector(self.config, self.memory)
            return {
                "ok": True,
                "events": detector.recent_events(int(limit or 20)),
                "text": detector.format_recent(int(limit or 20)),
            }

        def anomaly_mark_process_expected(self, process_name, expected=True):
            detector = get_anomaly_detector(self.config, self.memory)
            return detector.mark_process_expected(str(process_name), bool(expected), reason="user_tool")

        def anomaly_acknowledge(self, event_id=None):
            detector = get_anomaly_detector(self.config, self.memory)
            return detector.acknowledge(event_id=int(event_id) if event_id is not None else None)

        LocalTools.anomaly_status = anomaly_status
        LocalTools.anomaly_recent = anomaly_recent
        LocalTools.anomaly_mark_process_expected = anomaly_mark_process_expected
        LocalTools.anomaly_acknowledge = anomaly_acknowledge
        LocalTools._nova_anomaly_patched = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_anomaly", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        names = {"anomaly_status", "anomaly_recent", "anomaly_mark_process_expected", "anomaly_acknowledge"}
        cues = (
            "anomalia", "anomalía", "algo raro", "comportamiento extraño", "consumo extraño",
            "proceso extraño", "proceso raro", "pico de cpu", "pico de ram", "crash repetido",
            "marca este proceso como normal", "marca este proceso como esperado", "ignora este proceso",
        )

        def selector(text):
            rows = list(original_selector(text))
            present = {x["function"]["name"] for x in rows}
            if any(cue in (text or "").casefold() for cue in cues):
                rows += [by_name[n] for n in names if n in by_name and n not in present]
            return rows

        selector._nova_anomaly = True
        mod.select_tool_schemas = selector

    return mod
