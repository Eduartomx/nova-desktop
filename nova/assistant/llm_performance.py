from __future__ import annotations

"""Telemetría técnica local para las llamadas a Ollama de Nova.

No persiste prompts, respuestas, argumentos de herramientas, títulos de ventana
ni secretos. Solo guarda duraciones, conteos, tamaño aproximado de contexto,
modelo, resultado y muestras puntuales de GPU/VRAM cuando están disponibles.
"""

import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


DEFAULT_LLM_PERFORMANCE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_events": 1800,
    "gpu_sampling": True,
    "gpu_sample_timeout_seconds": 1.2,
    "cold_start_ms": 750,
    "slow_response_ms": 6000,
    "prompt_heavy_ratio": 0.30,
    "generation_heavy_ratio": 0.45,
    "gpu_pressure_percent": 85.0,
    "benchmark_max_tokens": 64,
}


def _ns_to_ms(value: Any) -> float:
    try:
        return round(float(value or 0) / 1_000_000.0, 3)
    except Exception:
        return 0.0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


class LLMPerformanceMonitor:
    def __init__(self, db_path: Path, config: dict[str, Any] | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = dict(DEFAULT_LLM_PERFORMANCE_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        self.session_id = uuid.uuid4().hex[:16]
        self.session_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._lock = Lock()
        self._nvidia_smi: str | None | bool = None
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    wall_ms REAL NOT NULL DEFAULT 0,
                    server_total_ms REAL NOT NULL DEFAULT 0,
                    load_ms REAL NOT NULL DEFAULT 0,
                    prompt_eval_ms REAL NOT NULL DEFAULT 0,
                    eval_ms REAL NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    prompt_tps REAL NOT NULL DEFAULT 0,
                    eval_tps REAL NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    tool_count INTEGER NOT NULL DEFAULT 0,
                    prompt_chars INTEGER NOT NULL DEFAULT 0,
                    system_chars INTEGER NOT NULL DEFAULT 0,
                    history_messages INTEGER NOT NULL DEFAULT 0,
                    gpu_before_util REAL,
                    gpu_after_util REAL,
                    vram_before_mb REAL,
                    vram_after_mb REAL,
                    vram_total_mb REAL,
                    success INTEGER NOT NULL DEFAULT 1,
                    error_type TEXT NOT NULL DEFAULT '',
                    done_reason TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_label ON llm_calls(label)")

    @staticmethod
    def context_metrics(messages: list[dict[str, Any]] | None, tools: list[dict[str, Any]] | None) -> dict[str, int]:
        rows = list(messages or [])
        prompt_chars = 0
        system_chars = 0
        for row in rows:
            content = row.get("content", "") if isinstance(row, dict) else ""
            if isinstance(content, str):
                size = len(content)
            else:
                size = len(str(content))
            prompt_chars += size
            if isinstance(row, dict) and str(row.get("role") or "") == "system":
                system_chars += size
        # Aproximación deliberadamente estructural: no se guarda ningún texto.
        history_messages = max(0, len(rows) - 2) if rows else 0
        return {
            "message_count": len(rows),
            "tool_count": len(list(tools or [])),
            "prompt_chars": prompt_chars,
            "system_chars": system_chars,
            "history_messages": history_messages,
        }

    def sample_gpu(self) -> dict[str, float] | None:
        if not self.enabled or not bool(self.config.get("gpu_sampling", True)):
            return None
        if self._nvidia_smi is False:
            return None
        if self._nvidia_smi is None:
            self._nvidia_smi = shutil.which("nvidia-smi") or False
        if not self._nvidia_smi:
            return None
        try:
            cp = subprocess.run(
                [
                    str(self._nvidia_smi),
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=float(self.config.get("gpu_sample_timeout_seconds", 1.2) or 1.2),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if cp.returncode != 0 or not cp.stdout.strip():
                return None
            parts = [x.strip() for x in cp.stdout.splitlines()[0].split(",")]
            if len(parts) < 3:
                return None
            return {
                "utilization": float(parts[0]),
                "vram_used_mb": float(parts[1]),
                "vram_total_mb": float(parts[2]),
            }
        except Exception:
            return None

    def _insert(self, row: dict[str, Any]):
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO llm_calls(
                        session_id,model,label,wall_ms,server_total_ms,load_ms,prompt_eval_ms,eval_ms,
                        prompt_tokens,output_tokens,prompt_tps,eval_tps,message_count,tool_count,prompt_chars,
                        system_chars,history_messages,gpu_before_util,gpu_after_util,vram_before_mb,vram_after_mb,
                        vram_total_mb,success,error_type,done_reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.session_id,
                        str(row.get("model") or "")[:120],
                        str(row.get("label") or "")[:80],
                        round(_safe_float(row.get("wall_ms")), 3),
                        round(_safe_float(row.get("server_total_ms")), 3),
                        round(_safe_float(row.get("load_ms")), 3),
                        round(_safe_float(row.get("prompt_eval_ms")), 3),
                        round(_safe_float(row.get("eval_ms")), 3),
                        _safe_int(row.get("prompt_tokens")),
                        _safe_int(row.get("output_tokens")),
                        round(_safe_float(row.get("prompt_tps")), 3),
                        round(_safe_float(row.get("eval_tps")), 3),
                        _safe_int(row.get("message_count")),
                        _safe_int(row.get("tool_count")),
                        _safe_int(row.get("prompt_chars")),
                        _safe_int(row.get("system_chars")),
                        _safe_int(row.get("history_messages")),
                        row.get("gpu_before_util"), row.get("gpu_after_util"),
                        row.get("vram_before_mb"), row.get("vram_after_mb"), row.get("vram_total_mb"),
                        1 if row.get("success", True) else 0,
                        str(row.get("error_type") or "")[:120],
                        str(row.get("done_reason") or "")[:120],
                    ),
                )
                max_events = max(200, int(self.config.get("max_events", 1800) or 1800))
                conn.execute(
                    "DELETE FROM llm_calls WHERE id <= (SELECT MAX(id)-? FROM llm_calls)",
                    (max_events,),
                )
        except Exception:
            # La observabilidad jamás puede romper una inferencia.
            pass

    def record_success(
        self,
        *,
        model: str,
        label: str,
        wall_ms: float,
        response: dict[str, Any],
        context: dict[str, int],
        gpu_before: dict[str, float] | None,
        gpu_after: dict[str, float] | None,
    ) -> dict[str, Any]:
        server_total_ms = _ns_to_ms(response.get("total_duration"))
        load_ms = _ns_to_ms(response.get("load_duration"))
        prompt_eval_ms = _ns_to_ms(response.get("prompt_eval_duration"))
        eval_ms = _ns_to_ms(response.get("eval_duration"))
        prompt_tokens = _safe_int(response.get("prompt_eval_count"))
        output_tokens = _safe_int(response.get("eval_count"))
        prompt_tps = (prompt_tokens / (prompt_eval_ms / 1000.0)) if prompt_tokens and prompt_eval_ms > 0 else 0.0
        eval_tps = (output_tokens / (eval_ms / 1000.0)) if output_tokens and eval_ms > 0 else 0.0
        row = {
            "model": model,
            "label": label,
            "wall_ms": wall_ms,
            "server_total_ms": server_total_ms,
            "load_ms": load_ms,
            "prompt_eval_ms": prompt_eval_ms,
            "eval_ms": eval_ms,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "prompt_tps": prompt_tps,
            "eval_tps": eval_tps,
            **context,
            "gpu_before_util": (gpu_before or {}).get("utilization"),
            "gpu_after_util": (gpu_after or {}).get("utilization"),
            "vram_before_mb": (gpu_before or {}).get("vram_used_mb"),
            "vram_after_mb": (gpu_after or {}).get("vram_used_mb"),
            "vram_total_mb": (gpu_after or gpu_before or {}).get("vram_total_mb"),
            "success": True,
            "error_type": "",
            "done_reason": str(response.get("done_reason") or ""),
        }
        self._insert(row)
        return row

    def record_failure(
        self,
        *,
        model: str,
        label: str,
        wall_ms: float,
        context: dict[str, int],
        gpu_before: dict[str, float] | None,
        gpu_after: dict[str, float] | None,
        error_type: str,
    ) -> dict[str, Any]:
        row = {
            "model": model,
            "label": label,
            "wall_ms": wall_ms,
            "server_total_ms": 0,
            "load_ms": 0,
            "prompt_eval_ms": 0,
            "eval_ms": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "prompt_tps": 0,
            "eval_tps": 0,
            **context,
            "gpu_before_util": (gpu_before or {}).get("utilization"),
            "gpu_after_util": (gpu_after or {}).get("utilization"),
            "vram_before_mb": (gpu_before or {}).get("vram_used_mb"),
            "vram_after_mb": (gpu_after or {}).get("vram_used_mb"),
            "vram_total_mb": (gpu_after or gpu_before or {}).get("vram_total_mb"),
            "success": False,
            "error_type": str(error_type or "")[:120],
            "done_reason": "",
        }
        self._insert(row)
        return row

    def _rows(self, hours: float = 24, session_only: bool = False, label: str | None = None) -> list[dict[str, Any]]:
        hours = max(0.01, min(float(hours or 24), 24 * 30))
        where = ["created_at >= datetime('now', ?)"]
        params: list[Any] = [f"-{hours} hours"]
        if session_only:
            where.append("session_id=?")
            params.append(self.session_id)
        if label:
            where.append("label=?")
            params.append(str(label))
        sql = "SELECT * FROM llm_calls WHERE " + " AND ".join(where) + " ORDER BY id DESC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def summary(self, hours: float = 24, session_only: bool = False, label: str | None = None) -> dict[str, Any]:
        rows = self._rows(hours=hours, session_only=session_only, label=label)
        if not rows:
            return {
                "ok": True,
                "hours": hours,
                "session_only": session_only,
                "session_id": self.session_id,
                "calls": 0,
                "failures": 0,
                "cause_codes": [],
                "rows": [],
            }

        def avg(key: str, only_positive: bool = False) -> float:
            values = [_safe_float(row.get(key)) for row in rows]
            if only_positive:
                values = [x for x in values if x > 0]
            return round(sum(values) / len(values), 2) if values else 0.0

        calls = len(rows)
        failures = sum(1 for row in rows if not bool(row.get("success")))
        max_wall = round(max(_safe_float(row.get("wall_ms")) for row in rows), 2)
        avg_wall = avg("wall_ms")
        avg_server = avg("server_total_ms", True)
        avg_load = avg("load_ms", True)
        avg_prompt_eval = avg("prompt_eval_ms", True)
        avg_eval = avg("eval_ms", True)
        avg_prompt_tokens = avg("prompt_tokens", True)
        avg_output_tokens = avg("output_tokens", True)
        avg_prompt_tps = avg("prompt_tps", True)
        avg_eval_tps = avg("eval_tps", True)
        avg_message_count = avg("message_count")
        avg_tool_count = avg("tool_count")
        avg_prompt_chars = avg("prompt_chars")
        cold_threshold = float(self.config.get("cold_start_ms", 750) or 750)
        cold_starts = sum(1 for row in rows if _safe_float(row.get("load_ms")) >= cold_threshold)

        vram_percents: list[float] = []
        gpu_utils: list[float] = []
        for row in rows:
            total = _safe_float(row.get("vram_total_mb"))
            for key in ("vram_before_mb", "vram_after_mb"):
                used = _safe_float(row.get(key))
                if total > 0 and used > 0:
                    vram_percents.append(used * 100.0 / total)
            for key in ("gpu_before_util", "gpu_after_util"):
                value = _safe_float(row.get(key))
                if value >= 0:
                    gpu_utils.append(value)
        avg_vram_percent = round(sum(vram_percents) / len(vram_percents), 1) if vram_percents else 0.0
        max_vram_percent = round(max(vram_percents), 1) if vram_percents else 0.0
        avg_gpu_util = round(sum(gpu_utils) / len(gpu_utils), 1) if gpu_utils else 0.0

        causes: list[str] = []
        if failures and failures / calls >= 0.20:
            causes.append("unstable_or_timeout")
        if cold_starts and cold_starts / calls >= 0.25:
            causes.append("cold_start")
        if avg_server > 0:
            if avg_prompt_eval >= 800 and avg_prompt_eval / avg_server >= float(self.config.get("prompt_heavy_ratio", 0.30)):
                causes.append("prompt_heavy")
            if avg_eval >= 1000 and avg_eval / avg_server >= float(self.config.get("generation_heavy_ratio", 0.45)):
                causes.append("generation_heavy")
        pressure = float(self.config.get("gpu_pressure_percent", 85.0) or 85.0)
        if max_vram_percent >= pressure:
            causes.append("gpu_memory_pressure")
        if avg_tool_count >= 12 and avg_prompt_tokens >= 2500:
            causes.append("tool_context_heavy")
        if avg_wall >= float(self.config.get("slow_response_ms", 6000) or 6000) and not causes:
            causes.append("unattributed_slow")

        models: dict[str, int] = {}
        for row in rows:
            model = str(row.get("model") or "?")
            models[model] = models.get(model, 0) + 1

        return {
            "ok": True,
            "hours": hours,
            "session_only": session_only,
            "session_id": self.session_id,
            "calls": calls,
            "failures": failures,
            "avg_wall_ms": avg_wall,
            "max_wall_ms": max_wall,
            "avg_server_ms": avg_server,
            "avg_load_ms": avg_load,
            "avg_prompt_eval_ms": avg_prompt_eval,
            "avg_eval_ms": avg_eval,
            "avg_prompt_tokens": avg_prompt_tokens,
            "avg_output_tokens": avg_output_tokens,
            "avg_prompt_tps": avg_prompt_tps,
            "avg_eval_tps": avg_eval_tps,
            "avg_message_count": avg_message_count,
            "avg_tool_count": avg_tool_count,
            "avg_prompt_chars": avg_prompt_chars,
            "cold_starts": cold_starts,
            "avg_vram_percent": avg_vram_percent,
            "max_vram_percent": max_vram_percent,
            "avg_gpu_util": avg_gpu_util,
            "cause_codes": causes,
            "models": models,
            "rows": rows[:12],
        }

    def windows(self) -> dict[str, dict[str, Any]]:
        return {
            "session": self.summary(hours=24 * 30, session_only=True),
            "15m": self.summary(hours=0.25),
            "1h": self.summary(hours=1),
            "24h": self.summary(hours=24),
        }

    @staticmethod
    def _cause_text(code: str) -> str:
        return {
            "unstable_or_timeout": "hay una proporción relevante de fallos/timeouts",
            "cold_start": "una parte importante del tiempo corresponde a carga/cold start del modelo",
            "prompt_heavy": "la evaluación del prompt/contexto consume una fracción alta del tiempo",
            "generation_heavy": "la mayor parte del tiempo se va en generar tokens",
            "gpu_memory_pressure": "la VRAM llegó a una zona de presión alta durante las inferencias",
            "tool_context_heavy": "se están exponiendo muchas herramientas junto con un prompt grande",
            "unattributed_slow": "la llamada es lenta pero las métricas disponibles no aíslan todavía una causa dominante",
        }.get(code, code.replace("_", " "))

    @classmethod
    def format_summary(cls, report: dict[str, Any], title: str = "Rendimiento LLM") -> str:
        if not report or not report.get("calls"):
            return f"{title}: todavía no hay llamadas de Ollama medidas en esta ventana."
        model_text = ", ".join(f"{name}×{count}" for name, count in (report.get("models") or {}).items())
        lines = [
            title,
            f"- Modelo: {model_text or '?'} · {report.get('calls')} llamadas · {report.get('failures')} fallos",
            f"- Tiempo: {report.get('avg_wall_ms')} ms prom. · {report.get('max_wall_ms')} ms máx. · servidor {report.get('avg_server_ms')} ms prom.",
            f"- Carga: {report.get('avg_load_ms')} ms · prompt eval: {report.get('avg_prompt_eval_ms')} ms · generación: {report.get('avg_eval_ms')} ms",
            f"- Tokens: prompt {report.get('avg_prompt_tokens')} prom. · salida {report.get('avg_output_tokens')} prom. · generación {report.get('avg_eval_tps')} tok/s",
            f"- Contexto: {report.get('avg_message_count')} mensajes · {report.get('avg_tool_count')} tools · {report.get('avg_prompt_chars')} caracteres aprox.",
        ]
        if report.get("max_vram_percent"):
            lines.append(
                f"- GPU: {report.get('avg_gpu_util')}% util. puntual prom. · VRAM {report.get('avg_vram_percent')}% prom. / {report.get('max_vram_percent')}% máx."
            )
        causes = list(report.get("cause_codes") or [])
        if causes:
            lines.append("- Causa probable: " + "; ".join(cls._cause_text(code) for code in causes) + ".")
        else:
            lines.append("- No aparece un cuello de botella dominante con las muestras disponibles.")
        return "\n".join(lines)


_INSTANCE: LLMPerformanceMonitor | None = None


def get_llm_performance(config: dict[str, Any] | None = None) -> LLMPerformanceMonitor:
    global _INSTANCE
    root = Path(__file__).resolve().parent.parent
    cfg = (config or {}).get("llm_performance", {}) if isinstance(config, dict) else {}
    if _INSTANCE is None:
        _INSTANCE = LLMPerformanceMonitor(root / "data" / "llm_performance.db", cfg)
    elif isinstance(cfg, dict) and cfg:
        merged = dict(DEFAULT_LLM_PERFORMANCE_CONFIG)
        merged.update(cfg)
        _INSTANCE.config = merged
    return _INSTANCE
