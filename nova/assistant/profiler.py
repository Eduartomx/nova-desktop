from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any


DEFAULT_PROFILER_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_events": 5000,
    "slow_ms": 1200,
    "summary_hours": 24,
}


class PerformanceProfiler:
    """Profiler local y liviano para Nova.

    Guarda únicamente nombre de operación, duración, éxito y metadatos técnicos
    pequeños. No registra prompts, mensajes, secretos ni contenido de archivos.
    """

    def __init__(self, db_path: Path, config: dict[str, Any] | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = dict(DEFAULT_PROFILER_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        self.session_id = uuid.uuid4().hex[:16]
        self._lock = Lock()
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _connection(self):
        # Igual que assistant.db: sqlite3.Connection.__exit__ no cierra el
        # handle. En Windows eso puede dejar el archivo bloqueado hasta GC.
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS performance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    success INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    session_id TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(performance_events)").fetchall()}
            if "session_id" not in columns:
                conn.execute("ALTER TABLE performance_events ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_operation ON performance_events(operation)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_created ON performance_events(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_session ON performance_events(session_id)")

    def record(self, operation: str, duration_ms: float, success: bool = True, metadata: dict[str, Any] | None = None):
        if not self.enabled:
            return
        safe_meta: dict[str, Any] = {}
        for key, value in (metadata or {}).items():
            if key.casefold() in {"prompt", "content", "message", "text", "token", "secret", "password", "api_key"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe_meta[str(key)[:64]] = value if not isinstance(value, str) else value[:180]
        try:
            with self._lock, self._connection() as conn:
                conn.execute(
                    "INSERT INTO performance_events(operation,duration_ms,success,metadata_json,session_id) VALUES (?,?,?,?,?)",
                    (
                        str(operation)[:120],
                        round(float(duration_ms), 3),
                        1 if success else 0,
                        json.dumps(safe_meta, ensure_ascii=False, separators=(",", ":")),
                        self.session_id,
                    ),
                )
                max_events = max(200, int(self.config.get("max_events", 5000)))
                conn.execute(
                    "DELETE FROM performance_events WHERE id <= (SELECT MAX(id)-? FROM performance_events)",
                    (max_events,),
                )
        except Exception:
            # El profiler jamás debe romper la operación que está observando.
            pass

    @contextmanager
    def measure(self, operation: str, metadata: dict[str, Any] | None = None):
        started = time.perf_counter()
        success = False
        try:
            yield
            success = True
        finally:
            self.record(operation, (time.perf_counter() - started) * 1000.0, success, metadata)

    def summary(self, hours: float | None = None, session_only: bool = False) -> dict[str, Any]:
        hours = float(hours if hours is not None else self.config.get("summary_hours", 24))
        hours = max(0.01, min(hours, 24 * 30))
        modifier = f"-{hours} hours"
        where = "created_at >= datetime('now', ?)"
        params: list[Any] = [modifier]
        if session_only:
            where += " AND session_id=?"
            params.append(self.session_id)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT operation,
                       COUNT(*) AS calls,
                       ROUND(AVG(duration_ms),2) AS avg_ms,
                       ROUND(MAX(duration_ms),2) AS max_ms,
                       ROUND(SUM(duration_ms),2) AS total_ms,
                       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS failures
                FROM performance_events
                WHERE {where}
                GROUP BY operation
                ORDER BY total_ms DESC
                """,
                tuple(params),
            ).fetchall()
        operations = [dict(row) for row in rows]
        slow_ms = float(self.config.get("slow_ms", 1200))
        slow = [row for row in operations if float(row.get("avg_ms") or 0) >= slow_ms]
        return {
            "ok": True,
            "hours": hours,
            "session_only": bool(session_only),
            "session_id": self.session_id,
            "operations": operations,
            "slow_operations": slow,
            "events": sum(int(row.get("calls") or 0) for row in operations),
        }

    def windows(self) -> dict[str, dict[str, Any]]:
        return {
            "session": self.summary(hours=24 * 30, session_only=True),
            "15m": self.summary(hours=0.25),
            "1h": self.summary(hours=1),
            "24h": self.summary(hours=24),
        }

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id,operation,duration_ms,success,metadata_json,session_id,created_at FROM performance_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except Exception:
                item["metadata"] = {}
            item["success"] = bool(item.get("success"))
            out.append(item)
        return out

    @staticmethod
    def format_summary(report: dict[str, Any], title: str | None = None) -> str:
        rows = list(report.get("operations") or [])
        if not rows:
            return "Profiler local: todavía no hay suficientes operaciones registradas en esta ventana."
        if title:
            heading = title
        elif report.get("session_only"):
            heading = "Rendimiento de Nova · sesión actual"
        else:
            heading = f"Rendimiento de Nova · últimas {report.get('hours', 24):g} h"
        lines = [heading, ""]
        for row in rows[:12]:
            lines.append(
                f"- {row.get('operation')}: {row.get('avg_ms')} ms prom. · "
                f"{row.get('max_ms')} ms máx. · {row.get('calls')} llamadas"
                + (f" · {row.get('failures')} fallos" if row.get("failures") else "")
            )
        slow = list(report.get("slow_operations") or [])
        if slow:
            lines += ["", "Cuellos de botella probables:"]
            lines += [f"- {x.get('operation')} ({x.get('avg_ms')} ms promedio)" for x in slow[:6]]
        else:
            lines += ["", "No hay una operación con promedio por encima del umbral de lentitud configurado."]
        return "\n".join(lines)

    @classmethod
    def format_windows(cls, windows: dict[str, dict[str, Any]]) -> str:
        labels = (("session", "Sesión actual"), ("15m", "Últimos 15 min"), ("1h", "Última hora"), ("24h", "Últimas 24 h"))
        lines = ["Rendimiento por ventana"]
        for key, label in labels:
            report = windows.get(key) or {}
            operations = list(report.get("operations") or [])
            agent = next((row for row in operations if row.get("operation") == "agent.total"), None)
            llm = next((row for row in operations if row.get("operation") == "llm.ollama_chat"), None)
            if not operations:
                lines.append(f"- {label}: sin eventos")
                continue
            parts = [f"{report.get('events', 0)} eventos"]
            if agent:
                parts.append(f"agent {agent.get('avg_ms')} ms")
            if llm:
                parts.append(f"LLM {llm.get('avg_ms')} ms")
            lines.append(f"- {label}: " + " · ".join(parts))
        return "\n".join(lines)


_INSTANCE: PerformanceProfiler | None = None


def get_profiler(config: dict[str, Any] | None = None) -> PerformanceProfiler:
    global _INSTANCE
    root = Path(__file__).resolve().parent.parent
    cfg = (config or {}).get("performance_profiler", {}) if isinstance(config, dict) else {}
    if _INSTANCE is None:
        _INSTANCE = PerformanceProfiler(root / "data" / "performance.db", cfg)
    elif isinstance(cfg, dict) and cfg:
        merged = dict(DEFAULT_PROFILER_CONFIG)
        merged.update(cfg)
        _INSTANCE.config = merged
    return _INSTANCE
