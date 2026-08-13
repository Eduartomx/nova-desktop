from __future__ import annotations

"""Benchmark explícito y acotado para el LLM local de Nova.

Solo se ejecuta cuando el usuario lo solicita directamente. No guarda ni devuelve
el contenido generado por el modelo; utiliza las métricas técnicas que Ollama ya
incluye en /api/chat.
"""

from typing import Any


_SYSTEM = (
    "Eres un benchmark local de rendimiento. Sigue la instrucción de forma muy breve. "
    "No uses información externa ni hagas preguntas adicionales."
)


def _tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "system_status",
                "description": "Lee CPU y RAM del equipo.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]


def _case_text(label: str) -> str:
    return {
        "benchmark.fast": "Respuesta corta",
        "benchmark.normal": "Razonamiento breve",
        "benchmark.tools": "Selección con tools",
    }.get(label, label)


def run_llm_benchmark(agent) -> dict[str, Any]:
    cfg = getattr(agent, "config", {}) if isinstance(getattr(agent, "config", {}), dict) else {}
    perf_cfg = cfg.get("llm_performance", {}) if isinstance(cfg.get("llm_performance", {}), dict) else {}
    max_tokens = max(16, min(int(perf_cfg.get("benchmark_max_tokens", 64) or 64), 128))
    cases = [
        {
            "label": "benchmark.fast",
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "Responde solamente con OK."},
            ],
            "tools": None,
            "num_predict": 16,
        },
        {
            "label": "benchmark.normal",
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "Explica en una sola frase qué hace un controlador PID."},
            ],
            "tools": None,
            "num_predict": max_tokens,
        },
        {
            "label": "benchmark.tools",
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "Para conocer CPU y RAM, selecciona la herramienta system_status."},
            ],
            "tools": _tool_schema(),
            "num_predict": max_tokens,
        },
    ]

    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            agent._ollama_chat(
                case["messages"],
                tools=case["tools"],
                options_override={"num_predict": case["num_predict"]},
                performance_label=case["label"],
            )
            metrics = dict(getattr(agent, "_last_llm_metrics", {}) or {})
            metrics["label"] = case["label"]
            metrics["ok"] = bool(metrics.get("success", True))
            results.append(metrics)
        except Exception as exc:
            metrics = dict(getattr(agent, "_last_llm_metrics", {}) or {})
            metrics.update({"label": case["label"], "ok": False, "error_type": type(exc).__name__})
            results.append(metrics)

    return {
        "ok": any(bool(row.get("ok")) for row in results),
        "model": str(getattr(agent, "model", "")),
        "cases": results,
    }


def _fmt_ms(value: Any) -> str:
    try:
        return f"{float(value or 0):.0f} ms"
    except Exception:
        return "?"


def format_llm_benchmark(report: dict[str, Any]) -> str:
    lines = [f"Nova LLM Benchmark · {report.get('model') or '?'}"]
    for row in report.get("cases") or []:
        name = _case_text(str(row.get("label") or ""))
        if not row.get("ok"):
            lines.append(f"- {name}: FALLÓ ({row.get('error_type') or 'error desconocido'})")
            continue
        parts = [
            f"total {_fmt_ms(row.get('wall_ms'))}",
            f"carga {_fmt_ms(row.get('load_ms'))}",
            f"prompt {_fmt_ms(row.get('prompt_eval_ms'))}",
            f"generación {_fmt_ms(row.get('eval_ms'))}",
        ]
        try:
            tps = float(row.get("eval_tps") or 0)
        except Exception:
            tps = 0.0
        if tps > 0:
            parts.append(f"{tps:.1f} tok/s")
        before = row.get("vram_before_mb")
        after = row.get("vram_after_mb")
        total = row.get("vram_total_mb")
        if before is not None and after is not None and total:
            parts.append(f"VRAM {float(before):.0f}→{float(after):.0f}/{float(total):.0f} MB")
        lines.append(f"- {name}: " + " · ".join(parts))

    successes = [row for row in report.get("cases") or [] if row.get("ok")]
    if successes:
        slowest = max(successes, key=lambda row: float(row.get("wall_ms") or 0))
        lines.append(f"Prueba más lenta: {_case_text(str(slowest.get('label') or ''))} ({_fmt_ms(slowest.get('wall_ms'))}).")
    lines.append("El benchmark es local y no envía prompts ni resultados a servicios externos.")
    return "\n".join(lines)
