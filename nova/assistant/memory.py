from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _connection(self):
        """Open a short-lived SQLite connection and ALWAYS close it.

        sqlite3.Connection's own context manager commits/rolls back but does
        not close the underlying handle. On Windows that can leave the .db
        file locked until garbage collection. Nova uses this wrapper so every
        operation releases the file immediately.
        """
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
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    step_index INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    success_criteria TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '',
                    verifier TEXT NOT NULL DEFAULT '',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(task_id, step_index),
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
                """
            )

    def add_message(self, role: str, content: str):
        if not content.strip():
            return
        with self._lock, self._connection() as conn:
            conn.execute("INSERT INTO messages(role, content) VALUES (?, ?)", (role, content))

    def recent_messages(self, limit: int = 16) -> list[dict[str, str]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def set_memory(self, key: str, value: str):
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO memories(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (key.strip(), value.strip()),
            )

    def get_memories(self, limit: int = 30) -> list[tuple[str, str]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(r["key"], r["value"]) for r in rows]

    # ---------- Task Engine ----------
    def create_task(self, goal: str, plan: dict[str, Any], status: str = "planned") -> int:
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO tasks(goal, status, plan_json) VALUES (?, ?, ?)",
                (goal.strip(), status, json.dumps(plan, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def update_task(self, task_id: int, status: str | None = None, summary: str | None = None):
        fields = ["updated_at=CURRENT_TIMESTAMP"]
        values: list[Any] = []
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if summary is not None:
            fields.append("summary=?")
            values.append(summary)
        values.append(int(task_id))
        with self._lock, self._connection() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", values)

    def upsert_task_step(
        self,
        task_id: int,
        step_index: int,
        description: str,
        success_criteria: str,
        status: str = "pending",
        attempts: int = 0,
        result: str = "",
        verifier: str = "",
    ):
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_steps(task_id, step_index, description, success_criteria, status, attempts, result, verifier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, step_index) DO UPDATE SET
                    description=excluded.description,
                    success_criteria=excluded.success_criteria,
                    status=excluded.status,
                    attempts=excluded.attempts,
                    result=excluded.result,
                    verifier=excluded.verifier,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(task_id), int(step_index), description, success_criteria, status, int(attempts), result, verifier),
            )

    def update_task_plan(self, task_id: int, plan: dict[str, Any]):
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE tasks SET plan_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), int(task_id)),
            )

    def replace_task_steps(self, task_id: int, start_index: int, steps: list[dict[str, Any]]):
        """Reemplaza de forma atómica la parte pendiente de un plan replanificado."""
        start_index = max(1, int(start_index))
        with self._lock, self._connection() as conn:
            conn.execute(
                "DELETE FROM task_steps WHERE task_id=? AND step_index>=?",
                (int(task_id), start_index),
            )
            for item in steps:
                idx = int(item.get("index", start_index))
                conn.execute(
                    """
                    INSERT INTO task_steps(task_id, step_index, description, success_criteria, status, attempts, result, verifier)
                    VALUES (?, ?, ?, ?, 'pending', 0, '', '')
                    """,
                    (
                        int(task_id), idx, str(item.get("description", "")),
                        str(item.get("success_criteria", "")),
                    ),
                )

    def add_task_event(self, task_id: int, event_type: str, message: str, data: dict[str, Any] | None = None):
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO task_events(task_id, event_type, message, data_json) VALUES (?, ?, ?, ?)",
                (int(task_id), str(event_type), str(message), json.dumps(data or {}, ensure_ascii=False, default=str)),
            )

    def list_task_events(self, task_id: int, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, message, data_json, created_at
                FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?
                """,
                (int(task_id), limit),
            ).fetchall()
        out = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["data"] = json.loads(item.pop("data_json") or "{}")
            except Exception:
                item["data"] = {}
            out.append(item)
        return out

    def list_tasks(self, limit: int = 12) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, goal, status, summary, created_at, updated_at FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            task = conn.execute(
                "SELECT id, goal, status, plan_json, summary, created_at, updated_at FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
            if not task:
                return None
            steps = conn.execute(
                """
                SELECT step_index, description, success_criteria, status, attempts, result, verifier, updated_at
                FROM task_steps WHERE task_id=? ORDER BY step_index
                """,
                (int(task_id),),
            ).fetchall()
        data = dict(task)
        try:
            data["plan"] = json.loads(data.pop("plan_json") or "{}")
        except Exception:
            data["plan"] = {}
        data["steps"] = [dict(r) for r in steps]
        data["events"] = self.list_task_events(int(task_id), 120)
        return data
