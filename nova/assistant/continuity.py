from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


TERMINAL_TASK_STATUSES = {"completed", "complete", "done", "success", "succeeded", "failed", "cancelled", "canceled"}
TERMINAL_SESSION_STATUSES = {"completed", "failed", "cancelled", "abandoned"}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, default=str)


def _loads(value: Any, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_list(values: Any, limit: int = 60) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


class ContinuityEngine:
    """Estado de trabajo persistente por workspace.

    Continuity no sustituye la memoria semántica: guarda estado temporal y
    accionable (qué se hizo, qué falta y dónde continuar), mientras Semantic
    Memory conserva hechos/decisiones recuperables por significado.
    """

    def __init__(self, memory, config: dict[str, Any] | None = None):
        self.memory = memory
        self.config: dict[str, Any] = {
            "enabled": True,
            "auto_checkpoint_tasks": True,
            "inject_context": True,
            "history_limit": 12,
            "max_items_per_checkpoint": 60,
        }
        self.configure(config or {})
        self.ensure_schema()

    def configure(self, config: dict[str, Any] | None = None):
        if isinstance(config, dict):
            self.config.update(config)
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def ensure_schema(self):
        with self.memory._lock, self.memory._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS continuity_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER,
                    task_id INTEGER,
                    title TEXT NOT NULL DEFAULT '',
                    goal TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    closed_at DATETIME,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS continuity_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    workspace_id INTEGER,
                    task_id INTEGER,
                    kind TEXT NOT NULL DEFAULT 'manual',
                    summary TEXT NOT NULL DEFAULT '',
                    completed_json TEXT NOT NULL DEFAULT '[]',
                    pending_json TEXT NOT NULL DEFAULT '[]',
                    files_json TEXT NOT NULL DEFAULT '[]',
                    decisions_json TEXT NOT NULL DEFAULT '[]',
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES continuity_sessions(id) ON DELETE CASCADE,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cont_sessions_workspace ON continuity_sessions(workspace_id, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cont_sessions_task ON continuity_sessions(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cont_checkpoints_workspace ON continuity_checkpoints(workspace_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cont_checkpoints_session ON continuity_checkpoints(session_id, id)")

    @staticmethod
    def _session(row) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def _checkpoint(row) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        item["completed"] = _loads(item.pop("completed_json", "[]"), [])
        item["pending"] = _loads(item.pop("pending_json", "[]"), [])
        item["files"] = _loads(item.pop("files_json", "[]"), [])
        item["decisions"] = _loads(item.pop("decisions_json", "[]"), [])
        item["errors"] = _loads(item.pop("errors_json", "[]"), [])
        item["metadata"] = _loads(item.pop("metadata_json", "{}"), {})
        return item

    def start(self, workspace_id: int | None = None, goal: str = "", task_id: int | None = None,
              title: str = "", force_new: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {"id": None, "workspace_id": workspace_id, "task_id": task_id, "status": "disabled"}
        self.ensure_schema()
        with self.memory._lock, self.memory._connection() as conn:
            row = None
            if task_id is not None:
                row = conn.execute(
                    "SELECT * FROM continuity_sessions WHERE task_id=? ORDER BY id DESC LIMIT 1",
                    (int(task_id),),
                ).fetchone()
            if row is None and not force_new:
                if workspace_id is None:
                    row = conn.execute(
                        "SELECT * FROM continuity_sessions WHERE workspace_id IS NULL AND status NOT IN ('completed','failed','cancelled','abandoned') ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM continuity_sessions WHERE workspace_id=? AND status NOT IN ('completed','failed','cancelled','abandoned') ORDER BY id DESC LIMIT 1",
                        (int(workspace_id),),
                    ).fetchone()
            if row:
                updates = []
                values: list[Any] = []
                if goal and not str(row["goal"] or "").strip():
                    updates.append("goal=?")
                    values.append(str(goal).strip())
                if title and not str(row["title"] or "").strip():
                    updates.append("title=?")
                    values.append(str(title).strip())
                if task_id is not None and row["task_id"] is None:
                    updates.append("task_id=?")
                    values.append(int(task_id))
                updates.append("updated_at=CURRENT_TIMESTAMP")
                values.append(int(row["id"]))
                conn.execute(f"UPDATE continuity_sessions SET {', '.join(updates)} WHERE id=?", values)
                row = conn.execute("SELECT * FROM continuity_sessions WHERE id=?", (int(row["id"]),)).fetchone()
                return dict(row)

            cur = conn.execute(
                "INSERT INTO continuity_sessions(workspace_id,task_id,title,goal,status) VALUES (?,?,?,?, 'active')",
                (workspace_id, task_id, str(title or "").strip(), str(goal or "").strip()),
            )
            row = conn.execute("SELECT * FROM continuity_sessions WHERE id=?", (int(cur.lastrowid),)).fetchone()
        return dict(row)

    def latest_session(self, workspace_id: int | None = None, any_if_none: bool = False) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.memory._lock, self.memory._connection() as conn:
            if workspace_id is None and any_if_none:
                row = conn.execute("SELECT * FROM continuity_sessions ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
            elif workspace_id is None:
                row = conn.execute("SELECT * FROM continuity_sessions WHERE workspace_id IS NULL ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM continuity_sessions WHERE workspace_id=? ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (int(workspace_id),),
                ).fetchone()
        return self._session(row)

    def latest_checkpoint(self, workspace_id: int | None = None, session_id: int | None = None,
                          any_if_none: bool = False) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.memory._lock, self.memory._connection() as conn:
            if session_id is not None:
                row = conn.execute(
                    "SELECT * FROM continuity_checkpoints WHERE session_id=? ORDER BY id DESC LIMIT 1",
                    (int(session_id),),
                ).fetchone()
            elif workspace_id is None and any_if_none:
                row = conn.execute("SELECT * FROM continuity_checkpoints ORDER BY id DESC LIMIT 1").fetchone()
            elif workspace_id is None:
                row = conn.execute("SELECT * FROM continuity_checkpoints WHERE workspace_id IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM continuity_checkpoints WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                    (int(workspace_id),),
                ).fetchone()
        return self._checkpoint(row)

    def checkpoint(self, workspace_id: int | None = None, summary: str = "", completed=None, pending=None,
                   files=None, decisions=None, errors=None, metadata: dict[str, Any] | None = None,
                   task_id: int | None = None, session_id: int | None = None, kind: str = "manual",
                   session_status: str | None = None, goal: str = "") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "error": "Continuity Engine está desactivado."}
        max_items = max(1, min(int(self.config.get("max_items_per_checkpoint", 60)), 200))
        session = None
        if session_id is not None:
            with self.memory._lock, self.memory._connection() as conn:
                row = conn.execute("SELECT * FROM continuity_sessions WHERE id=?", (int(session_id),)).fetchone()
            session = self._session(row)
        if session is None:
            session = self.start(workspace_id=workspace_id, goal=goal, task_id=task_id)
        sid = int(session["id"])
        workspace_id = session.get("workspace_id") if workspace_id is None else workspace_id
        task_id = session.get("task_id") if task_id is None else task_id
        completed = _clean_list(completed, max_items)
        pending = _clean_list(pending, max_items)
        files = _clean_list(files, max_items)
        decisions = _clean_list(decisions, max_items)
        errors = _clean_list(errors, max_items)
        meta = dict(metadata or {})
        meta.setdefault("captured_at", _now_iso())

        with self.memory._lock, self.memory._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO continuity_checkpoints(
                    session_id,workspace_id,task_id,kind,summary,completed_json,pending_json,
                    files_json,decisions_json,errors_json,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sid, workspace_id, task_id, str(kind or "manual"), str(summary or "").strip(),
                    _json(completed), _json(pending), _json(files), _json(decisions), _json(errors), _json(meta),
                ),
            )
            status = str(session_status or session.get("status") or "active").casefold()
            closed = status in TERMINAL_SESSION_STATUSES
            conn.execute(
                "UPDATE continuity_sessions SET status=?, updated_at=CURRENT_TIMESTAMP, closed_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE closed_at END WHERE id=?",
                (status, 1 if closed else 0, sid),
            )
            row = conn.execute("SELECT * FROM continuity_checkpoints WHERE id=?", (int(cur.lastrowid),)).fetchone()
        return {"ok": True, "session": self.latest_session(workspace_id), "checkpoint": self._checkpoint(row)}

    def checkpoint_from_task(self, task_id: int, reason: str = "task_update") -> dict[str, Any]:
        try:
            task = self.memory.get_task(int(task_id))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not task:
            return {"ok": False, "error": f"No existe la tarea #{task_id}."}

        workspace_id = task.get("workspace_id")
        status = str(task.get("status") or "active").casefold()
        steps = list(task.get("steps") or [])
        done_states = {"completed", "complete", "done", "success", "succeeded"}
        failed_states = {"failed", "error"}
        completed: list[str] = []
        pending: list[str] = []
        errors: list[str] = []
        for step in steps:
            desc = str(step.get("description") or f"Paso {step.get('step_index', '?')}").strip()
            result = str(step.get("result") or "").strip()
            state = str(step.get("status") or "pending").casefold()
            text = f"{desc}: {result}" if result else desc
            if state in done_states:
                completed.append(text)
            elif state in failed_states:
                errors.append(text)
                pending.append(desc)
            else:
                pending.append(desc)

        summary = str(task.get("summary") or "").strip()
        if not summary:
            summary = f"Tarea #{task_id} · {task.get('goal','')} · estado {status}"
        if status in {"completed", "complete", "done", "success", "succeeded"}:
            session_status = "completed"
            pending = []
        elif status in {"failed"}:
            session_status = "failed"
        elif status in {"cancelled", "canceled"}:
            session_status = "cancelled"
        elif status in {"paused", "blocked"}:
            session_status = "paused"
        else:
            session_status = "active"

        return self.checkpoint(
            workspace_id=workspace_id,
            task_id=int(task_id),
            goal=str(task.get("goal") or ""),
            summary=summary,
            completed=completed,
            pending=pending,
            errors=errors,
            metadata={"reason": reason, "task_status": status},
            kind="task",
            session_status=session_status,
        )

    def _open_tasks(self, workspace_id: int | None = None, limit: int = 12) -> list[dict[str, Any]]:
        with self.memory._lock, self.memory._connection() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "workspace_id" not in cols:
                rows = conn.execute(
                    "SELECT id,goal,status,summary,updated_at FROM tasks ORDER BY updated_at DESC,id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            elif workspace_id is None:
                rows = conn.execute(
                    "SELECT id,goal,status,summary,workspace_id,updated_at FROM tasks WHERE workspace_id IS NULL ORDER BY updated_at DESC,id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id,goal,status,summary,workspace_id,updated_at FROM tasks WHERE workspace_id=? ORDER BY updated_at DESC,id DESC LIMIT ?",
                    (int(workspace_id), int(limit)),
                ).fetchall()
        return [dict(r) for r in rows if str(r["status"] or "").casefold() not in TERMINAL_TASK_STATUSES]

    def resume(self, workspace_id: int | None = None, any_if_none: bool = False) -> dict[str, Any]:
        session = self.latest_session(workspace_id, any_if_none=any_if_none)
        checkpoint = self.latest_checkpoint(
            workspace_id=session.get("workspace_id") if session else workspace_id,
            session_id=int(session["id"]) if session else None,
            any_if_none=any_if_none,
        ) if session else self.latest_checkpoint(workspace_id, any_if_none=any_if_none)
        resolved_workspace = session.get("workspace_id") if session else workspace_id
        tasks = self._open_tasks(resolved_workspace, limit=12)
        return {
            "ok": bool(session or checkpoint or tasks),
            "workspace_id": resolved_workspace,
            "session": session,
            "checkpoint": checkpoint,
            "open_tasks": tasks,
            "compact": self.compact_context(session=session, checkpoint=checkpoint, open_tasks=tasks),
        }

    def pending(self, workspace_id: int | None = None, any_if_none: bool = False) -> dict[str, Any]:
        state = self.resume(workspace_id, any_if_none=any_if_none)
        cp = state.get("checkpoint") or {}
        pending = _clean_list(cp.get("pending"), 80)
        task_items = [f"#{t.get('id')} {t.get('status')}: {t.get('goal')}" for t in state.get("open_tasks", [])]
        merged = pending + [x for x in task_items if x not in pending]
        return {**state, "pending_items": merged}

    def history(self, workspace_id: int | None = None, limit: int | None = None, any_if_none: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or self.config.get("history_limit", 12)), 100))
        with self.memory._lock, self.memory._connection() as conn:
            if workspace_id is None and any_if_none:
                rows = conn.execute("SELECT * FROM continuity_checkpoints ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            elif workspace_id is None:
                rows = conn.execute("SELECT * FROM continuity_checkpoints WHERE workspace_id IS NULL ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM continuity_checkpoints WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
                    (int(workspace_id), limit),
                ).fetchall()
        return [self._checkpoint(r) for r in reversed(rows)]

    def close(self, workspace_id: int | None = None, status: str = "completed", summary: str = "") -> dict[str, Any]:
        session = self.latest_session(workspace_id, any_if_none=workspace_id is None)
        if not session:
            return {"ok": False, "error": "No hay una sesión de continuidad para cerrar."}
        if summary:
            self.checkpoint(
                workspace_id=session.get("workspace_id"), session_id=int(session["id"]), task_id=session.get("task_id"),
                summary=summary, kind="close", session_status=status,
            )
        else:
            with self.memory._lock, self.memory._connection() as conn:
                conn.execute(
                    "UPDATE continuity_sessions SET status=?,updated_at=CURRENT_TIMESTAMP,closed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(status or "completed").casefold(), int(session["id"])),
                )
        return {"ok": True, "session": self.latest_session(session.get("workspace_id"))}

    def stats(self) -> dict[str, int]:
        self.ensure_schema()
        with self.memory._lock, self.memory._connection() as conn:
            sessions = int(conn.execute("SELECT COUNT(*) AS n FROM continuity_sessions").fetchone()["n"])
            checkpoints = int(conn.execute("SELECT COUNT(*) AS n FROM continuity_checkpoints").fetchone()["n"])
            active = int(conn.execute(
                "SELECT COUNT(*) AS n FROM continuity_sessions WHERE status NOT IN ('completed','failed','cancelled','abandoned')"
            ).fetchone()["n"])
        return {"continuity_sessions": sessions, "continuity_checkpoints": checkpoints, "continuity_active": active}

    @staticmethod
    def compact_context(session=None, checkpoint=None, open_tasks=None) -> str:
        session = session or {}
        checkpoint = checkpoint or {}
        open_tasks = open_tasks or []
        if not session and not checkpoint and not open_tasks:
            return "(sin continuidad registrada)"
        lines: list[str] = []
        if session:
            goal = str(session.get("goal") or session.get("title") or "").strip()
            lines.append(f"Sesión #{session.get('id')} · {session.get('status','active')}" + (f" · objetivo: {goal}" if goal else ""))
        if checkpoint:
            if checkpoint.get("summary"):
                lines.append("Último checkpoint: " + str(checkpoint["summary"]))
            completed = checkpoint.get("completed") or []
            pending = checkpoint.get("pending") or []
            decisions = checkpoint.get("decisions") or []
            errors = checkpoint.get("errors") or []
            files = checkpoint.get("files") or []
            if completed:
                lines.append("Completado: " + " | ".join(str(x) for x in completed[:6]))
            if pending:
                lines.append("Pendiente: " + " | ".join(str(x) for x in pending[:6]))
            if decisions:
                lines.append("Decisiones: " + " | ".join(str(x) for x in decisions[:5]))
            if errors:
                lines.append("Errores/bloqueos: " + " | ".join(str(x) for x in errors[:5]))
            if files:
                lines.append("Archivos: " + " | ".join(str(x) for x in files[:6]))
        if open_tasks:
            lines.append("Tareas abiertas: " + " | ".join(f"#{t.get('id')} {t.get('status')}: {t.get('goal')}" for t in open_tasks[:5]))
        return "\n".join(lines)
