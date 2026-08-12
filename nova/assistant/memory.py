from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from .continuity import ContinuityEngine
from .semantic_memory import SemanticMemoryEngine


_TASK_CHECKPOINT_STATES = {
    "completed", "complete", "done", "success", "succeeded",
    "failed", "cancelled", "canceled", "paused", "blocked",
}
_EVENT_CHECKPOINT_TYPES = {
    "completed", "complete", "failed", "error", "blocked", "paused",
    "cancelled", "canceled", "replan", "replanned",
}


class MemoryStore:
    """Persistencia consolidada de Nova.

    Desde v0.6.7 Memory/Workspace/Semantic Memory/Continuity viven aquí de
    forma nativa. Ya no se añaden mediante monkey patches versionados.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.semantic_memory = SemanticMemoryEngine(self, config={"enabled": False})
        self.continuity = ContinuityEngine(self, config={"enabled": False})

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def _connection(self):
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
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
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL DEFAULT 'generic',
                    description TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'fact',
                    scope TEXT NOT NULL DEFAULT 'global',
                    workspace_id INTEGER,
                    importance REAL NOT NULL DEFAULT 0.5,
                    source TEXT NOT NULL DEFAULT 'user',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(key, workspace_id),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                )
            """)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "workspace_id" not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL")

            count = int(conn.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()["n"])
            if not count:
                for row in conn.execute("SELECT key, value FROM memories ORDER BY id").fetchall():
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_items(key,value,category,scope,workspace_id,importance,source) VALUES (?,?, 'fact','global',NULL,0.5,'legacy')",
                        (row["key"], row["value"]),
                    )

    # ---------- Conversación ----------
    def add_message(self, role: str, content: str):
        if not content.strip():
            return
        with self._lock, self._connection() as conn:
            conn.execute("INSERT INTO messages(role, content) VALUES (?, ?)", (role, content))

    def recent_messages(self, limit: int = 16) -> list[dict[str, str]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # ---------- Memoria ----------
    def set_memory(
        self,
        key: str,
        value: str,
        category: str = "fact",
        workspace_id: int | None = None,
        importance: float = 0.5,
        source: str = "user",
    ):
        key, value = key.strip(), value.strip()
        if not key or not value:
            return
        importance = max(0.0, min(float(importance), 1.0))
        scope = "workspace" if workspace_id is not None else "global"
        with self._lock, self._connection() as conn:
            if workspace_id is None:
                conn.execute(
                    """
                    INSERT INTO memories(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                    """,
                    (key, value),
                )
            row = conn.execute(
                "SELECT id FROM memory_items WHERE key=? AND ((workspace_id IS NULL AND ? IS NULL) OR workspace_id=?) LIMIT 1",
                (key, workspace_id, workspace_id),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE memory_items SET value=?,category=?,scope=?,importance=?,source=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (value, category or "fact", scope, importance, source or "user", int(row["id"])),
                )
            else:
                conn.execute(
                    "INSERT INTO memory_items(key,value,category,scope,workspace_id,importance,source) VALUES (?,?,?,?,?,?,?)",
                    (key, value, category or "fact", scope, workspace_id, importance, source or "user"),
                )
        try:
            self.semantic_memory.invalidate_by_key(key, workspace_id)
        except Exception:
            pass

    def get_memories(self, limit: int = 30) -> list[tuple[str, str]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT key, value FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [(r["key"], r["value"]) for r in rows]

    def recent_memory_items(self, limit: int = 12, workspace_id: int | None = None):
        limit = max(1, min(int(limit), 100))
        with self._lock, self._connection() as conn:
            if workspace_id is None:
                rows = conn.execute(
                    "SELECT * FROM memory_items WHERE workspace_id IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_items WHERE workspace_id IS NULL OR workspace_id=?
                    ORDER BY (workspace_id IS NOT NULL) DESC, importance DESC, updated_at DESC LIMIT ?
                    """,
                    (int(workspace_id), limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def search_memory_lexical(self, query: str, limit: int = 8, workspace_id: int | None = None):
        terms = [x for x in re.findall(r"[\wáéíóúüñ]+", (query or "").casefold()) if len(x) > 1]
        rows = self.recent_memory_items(120, workspace_id)
        scored = []
        for row in rows:
            hay = f"{row.get('key','')} {row.get('value','')} {row.get('category','')}".casefold()
            hits = sum(1 for t in terms if t in hay)
            phrase = 1 if query and query.casefold() in hay else 0
            ws_bonus = 1.25 if workspace_id is not None and row.get("workspace_id") == workspace_id else 0
            score = hits * 2 + phrase * 3 + ws_bonus + float(row.get("importance") or 0.5)
            if score > 0.5 or not terms:
                item = dict(row)
                item["score"] = round(score, 3)
                scored.append(item)
        scored.sort(key=lambda x: (x["score"], str(x.get("updated_at", ""))), reverse=True)
        return scored[: max(1, min(int(limit), 40))]

    def search_memory(self, query: str, limit: int = 8, workspace_id: int | None = None):
        engine = getattr(self, "semantic_memory", None)
        if engine is None:
            return self.search_memory_lexical(query, limit, workspace_id)
        return engine.search(query, limit, workspace_id, self.search_memory_lexical)

    def configure_semantic_memory(self, config=None, ollama_host=None):
        self.semantic_memory.configure(config or {}, ollama_host)
        return self.semantic_memory

    def semantic_status(self, workspace_id=None, refresh=False):
        return self.semantic_memory.status(workspace_id=workspace_id, refresh=bool(refresh))

    def semantic_reindex(self, workspace_id=None, force=False, limit=1000):
        return self.semantic_memory.reindex(workspace_id=workspace_id, force=bool(force), limit=limit)

    # ---------- Workspaces ----------
    @staticmethod
    def _normalize_workspace(row):
        if not row:
            return None
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except Exception:
            data["metadata"] = {}
        data["is_active"] = bool(data.get("is_active"))
        return data

    def create_workspace(self, name: str, path: str, kind: str = "generic", description: str = "",
                         metadata: dict | None = None, set_active: bool = True):
        with self._lock, self._connection() as conn:
            if set_active:
                conn.execute("UPDATE workspaces SET is_active=0")
            conn.execute("""
                INSERT INTO workspaces(name,path,kind,description,metadata_json,is_active)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET name=excluded.name,kind=excluded.kind,
                  description=excluded.description,metadata_json=excluded.metadata_json,
                  is_active=excluded.is_active,updated_at=CURRENT_TIMESTAMP
            """, (name.strip(), path.strip(), kind or "generic", description or "",
                    json.dumps(metadata or {}, ensure_ascii=False), 1 if set_active else 0))
            row = conn.execute("SELECT id FROM workspaces WHERE path=?", (path.strip(),)).fetchone()
            return int(row["id"])

    def get_workspace(self, workspace_id: int):
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id=?", (int(workspace_id),)).fetchone()
        return self._normalize_workspace(row)

    def list_workspaces(self, limit: int = 30):
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces ORDER BY is_active DESC, updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._normalize_workspace(r) for r in rows]

    def active_workspace(self):
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE is_active=1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        return self._normalize_workspace(row)

    def resolve_workspace(self, selector=None):
        if selector in (None, ""):
            return self.active_workspace()
        try:
            return self.get_workspace(int(selector))
        except (TypeError, ValueError):
            pass
        q = str(selector).strip()
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE lower(name)=lower(?) OR lower(path)=lower(?) ORDER BY is_active DESC LIMIT 1",
                (q, q),
            ).fetchone()
        return self._normalize_workspace(row)

    def set_active_workspace(self, selector):
        ws = self.resolve_workspace(selector)
        if not ws:
            return None
        with self._lock, self._connection() as conn:
            conn.execute("UPDATE workspaces SET is_active=0")
            conn.execute("UPDATE workspaces SET is_active=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(ws["id"]),))
        return self.get_workspace(int(ws["id"]))

    def clear_active_workspace(self):
        with self._lock, self._connection() as conn:
            conn.execute("UPDATE workspaces SET is_active=0")

    def update_workspace_metadata(self, workspace_id: int, metadata: dict, kind: str | None = None):
        with self._lock, self._connection() as conn:
            if kind:
                conn.execute(
                    "UPDATE workspaces SET metadata_json=?,kind=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False), kind, int(workspace_id)),
                )
            else:
                conn.execute(
                    "UPDATE workspaces SET metadata_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False), int(workspace_id)),
                )

    # ---------- Continuity ----------
    def configure_continuity(self, config=None):
        self.continuity.configure(config or {})
        self.continuity.ensure_schema()
        return self.continuity

    def continuity_resume(self, workspace_id=None, any_if_none=False):
        return self.continuity.resume(workspace_id=workspace_id, any_if_none=bool(any_if_none))

    def continuity_pending(self, workspace_id=None, any_if_none=False):
        return self.continuity.pending(workspace_id=workspace_id, any_if_none=bool(any_if_none))

    def continuity_history(self, workspace_id=None, limit=None, any_if_none=False):
        return self.continuity.history(workspace_id=workspace_id, limit=limit, any_if_none=bool(any_if_none))

    def continuity_checkpoint(self, **kwargs):
        return self.continuity.checkpoint(**kwargs)

    def continuity_close(self, workspace_id=None, status="completed", summary=""):
        return self.continuity.close(workspace_id=workspace_id, status=status, summary=summary)

    def _continuity_auto_enabled(self):
        return bool(self.continuity and self.continuity.enabled and self.continuity.config.get("auto_checkpoint_tasks", True))

    def _safe_task_checkpoint(self, task_id: int, reason: str):
        if not self._continuity_auto_enabled():
            return
        try:
            self.continuity.checkpoint_from_task(int(task_id), reason=reason)
        except Exception:
            pass

    # ---------- Task Engine ----------
    def create_task(self, goal: str, plan: dict[str, Any], status: str = "planned") -> int:
        active = self.active_workspace()
        with self._lock, self._connection() as conn:
            if active:
                cur = conn.execute(
                    "INSERT INTO tasks(goal,status,plan_json,workspace_id) VALUES (?,?,?,?)",
                    (goal.strip(), status, json.dumps(plan, ensure_ascii=False), int(active["id"])),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO tasks(goal,status,plan_json) VALUES (?,?,?)",
                    (goal.strip(), status, json.dumps(plan, ensure_ascii=False)),
                )
            task_id = int(cur.lastrowid)

        if self._continuity_auto_enabled():
            try:
                workspace_id = int(active["id"]) if active else None
                self.continuity.start(workspace_id=workspace_id, goal=goal, task_id=task_id, title=goal[:160])
                self.continuity.checkpoint(
                    workspace_id=workspace_id,
                    task_id=task_id,
                    goal=goal,
                    summary=f"Tarea #{task_id} creada: {goal}",
                    pending=[str(x.get("description") or "").strip() for x in (plan or {}).get("steps", []) if str(x.get("description") or "").strip()],
                    metadata={"reason": "task_created", "task_status": status},
                    kind="task",
                    session_status="active",
                )
            except Exception:
                pass
        return task_id

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
        state = str(status or "").casefold()
        if summary is not None or state in _TASK_CHECKPOINT_STATES:
            self._safe_task_checkpoint(int(task_id), "task_update")

    def upsert_task_step(self, task_id: int, step_index: int, description: str, success_criteria: str,
                         status: str = "pending", attempts: int = 0, result: str = "", verifier: str = ""):
        with self._lock, self._connection() as conn:
            conn.execute("""
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
            """, (int(task_id), int(step_index), description, success_criteria, status, int(attempts), result, verifier))
        if str(status or "").casefold() in _TASK_CHECKPOINT_STATES:
            self._safe_task_checkpoint(int(task_id), "task_step")

    def update_task_plan(self, task_id: int, plan: dict[str, Any]):
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE tasks SET plan_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), int(task_id)),
            )

    def replace_task_steps(self, task_id: int, start_index: int, steps: list[dict[str, Any]]):
        start_index = max(1, int(start_index))
        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM task_steps WHERE task_id=? AND step_index>=?", (int(task_id), start_index))
            for item in steps:
                idx = int(item.get("index", start_index))
                conn.execute("""
                    INSERT INTO task_steps(task_id, step_index, description, success_criteria, status, attempts, result, verifier)
                    VALUES (?, ?, ?, ?, 'pending', 0, '', '')
                """, (int(task_id), idx, str(item.get("description", "")), str(item.get("success_criteria", ""))))

    def add_task_event(self, task_id: int, event_type: str, message: str, data: dict[str, Any] | None = None):
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO task_events(task_id, event_type, message, data_json) VALUES (?, ?, ?, ?)",
                (int(task_id), str(event_type), str(message), json.dumps(data or {}, ensure_ascii=False, default=str)),
            )
        if str(event_type or "").casefold() in _EVENT_CHECKPOINT_TYPES:
            self._safe_task_checkpoint(int(task_id), f"task_event:{event_type}")

    def list_task_events(self, task_id: int, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock, self._connection() as conn:
            rows = conn.execute("""
                SELECT id, event_type, message, data_json, created_at
                FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?
            """, (int(task_id), limit)).fetchall()
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
                "SELECT id,goal,status,summary,workspace_id,created_at,updated_at FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            wid = item.get("workspace_id")
            item["workspace"] = self.get_workspace(int(wid)) if wid else None
            item["workspace_name"] = item["workspace"].get("name") if item.get("workspace") else None
            out.append(item)
        return out

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            task = conn.execute(
                "SELECT id,goal,status,plan_json,summary,workspace_id,created_at,updated_at FROM tasks WHERE id=?",
                (int(task_id),),
            ).fetchone()
            if not task:
                return None
            steps = conn.execute("""
                SELECT step_index,description,success_criteria,status,attempts,result,verifier,updated_at
                FROM task_steps WHERE task_id=? ORDER BY step_index
            """, (int(task_id),)).fetchall()
        data = dict(task)
        try:
            data["plan"] = json.loads(data.pop("plan_json") or "{}")
        except Exception:
            data["plan"] = {}
        data["steps"] = [dict(r) for r in steps]
        data["events"] = self.list_task_events(int(task_id), 120)
        wid = data.get("workspace_id")
        data["workspace"] = self.get_workspace(int(wid)) if wid else None
        data["workspace_name"] = data["workspace"].get("name") if data.get("workspace") else None
        return data

    def stats(self):
        with self._lock, self._connection() as conn:
            counts = {
                name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                for name in ("messages", "memory_items", "workspaces", "tasks")
            }
        try:
            size = round(self.db_path.stat().st_size / 1024 / 1024, 2)
        except OSError:
            size = 0.0
        base = {"db_size_mb": size, **counts}
        try:
            base.update(self.continuity.stats())
        except Exception:
            base.update({"continuity_sessions": 0, "continuity_checkpoints": 0, "continuity_active": 0})
        return base
