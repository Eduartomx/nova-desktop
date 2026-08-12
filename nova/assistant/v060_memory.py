from __future__ import annotations

import json
import re
from typing import Any


def install_memory_v060():
    """Extiende MemoryStore v0.5 sin reemplazar su implementación estable."""
    from . import memory as memory_mod

    MemoryStore = memory_mod.MemoryStore
    if getattr(MemoryStore, "_nova_v060_patched", False):
        return MemoryStore

    original_init = MemoryStore.__init__
    original_set_memory = MemoryStore.set_memory
    original_create_task = MemoryStore.create_task
    original_get_task = MemoryStore.get_task

    def migrate(self):
        with self._lock, self._connection() as conn:
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
            cols = {r['name'] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if 'workspace_id' not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL")
            count = conn.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()['n']
            if not count:
                for row in conn.execute("SELECT key, value FROM memories ORDER BY id").fetchall():
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_items(key,value,category,scope,workspace_id,importance,source) VALUES (?,?, 'fact','global',NULL,0.5,'legacy')",
                        (row['key'], row['value']),
                    )

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        migrate(self)

    def set_memory(self, key: str, value: str, category: str = 'fact', workspace_id: int | None = None,
                   importance: float = 0.5, source: str = 'user'):
        key, value = key.strip(), value.strip()
        if not key or not value:
            return
        if workspace_id is None:
            original_set_memory(self, key, value)
        scope = 'workspace' if workspace_id is not None else 'global'
        importance = max(0.0, min(float(importance), 1.0))
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT id FROM memory_items WHERE key=? AND ((workspace_id IS NULL AND ? IS NULL) OR workspace_id=?) LIMIT 1",
                (key, workspace_id, workspace_id),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE memory_items SET value=?,category=?,scope=?,importance=?,source=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (value, category or 'fact', scope, importance, source or 'user', int(row['id'])),
                )
            else:
                conn.execute(
                    "INSERT INTO memory_items(key,value,category,scope,workspace_id,importance,source) VALUES (?,?,?,?,?,?,?)",
                    (key, value, category or 'fact', scope, workspace_id, importance, source or 'user'),
                )

    def recent_memory_items(self, limit: int = 12, workspace_id: int | None = None):
        limit = max(1, min(int(limit), 100))
        with self._lock, self._connection() as conn:
            if workspace_id is None:
                rows = conn.execute("""
                    SELECT * FROM memory_items WHERE workspace_id IS NULL
                    ORDER BY importance DESC, updated_at DESC LIMIT ?
                """, (limit,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM memory_items WHERE workspace_id IS NULL OR workspace_id=?
                    ORDER BY (workspace_id IS NOT NULL) DESC, importance DESC, updated_at DESC LIMIT ?
                """, (int(workspace_id), limit)).fetchall()
        return [dict(r) for r in rows]

    def search_memory(self, query: str, limit: int = 8, workspace_id: int | None = None):
        """Ranking léxico local rápido; evita gastar inferencias para recuperar contexto."""
        terms = [x for x in re.findall(r"[\wáéíóúüñ]+", (query or '').casefold()) if len(x) > 1]
        rows = recent_memory_items(self, 120, workspace_id)
        scored = []
        for row in rows:
            hay = f"{row.get('key','')} {row.get('value','')} {row.get('category','')}".casefold()
            hits = sum(1 for t in terms if t in hay)
            phrase = 1 if query and query.casefold() in hay else 0
            ws_bonus = 1.25 if workspace_id is not None and row.get('workspace_id') == workspace_id else 0
            score = hits * 2 + phrase * 3 + ws_bonus + float(row.get('importance') or .5)
            if score > 0.5 or not terms:
                item = dict(row); item['score'] = round(score, 3); scored.append(item)
        scored.sort(key=lambda x: (x['score'], str(x.get('updated_at', ''))), reverse=True)
        return scored[:max(1, min(int(limit), 40))]

    def create_workspace(self, name: str, path: str, kind: str = 'generic', description: str = '',
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
            """, (name.strip(), path.strip(), kind or 'generic', description or '',
                    json.dumps(metadata or {}, ensure_ascii=False), 1 if set_active else 0))
            row = conn.execute("SELECT id FROM workspaces WHERE path=?", (path.strip(),)).fetchone()
            return int(row['id'])

    def normalize_ws(row):
        if not row:
            return None
        data = dict(row)
        try:
            data['metadata'] = json.loads(data.pop('metadata_json') or '{}')
        except Exception:
            data['metadata'] = {}
        data['is_active'] = bool(data.get('is_active'))
        return data

    def get_workspace(self, workspace_id: int):
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id=?", (int(workspace_id),)).fetchone()
        return normalize_ws(row)

    def list_workspaces(self, limit: int = 30):
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT * FROM workspaces ORDER BY is_active DESC, updated_at DESC LIMIT ?",
                                (max(1, min(int(limit), 100)),)).fetchall()
        return [normalize_ws(r) for r in rows]

    def active_workspace(self):
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE is_active=1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        return normalize_ws(row)

    def resolve_workspace(self, selector=None):
        if selector in (None, ''):
            return active_workspace(self)
        try:
            return get_workspace(self, int(selector))
        except (TypeError, ValueError):
            pass
        q = str(selector).strip()
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE lower(name)=lower(?) OR lower(path)=lower(?) ORDER BY is_active DESC LIMIT 1",
                               (q, q)).fetchone()
        return normalize_ws(row)

    def set_active_workspace(self, selector):
        ws = resolve_workspace(self, selector)
        if not ws:
            return None
        with self._lock, self._connection() as conn:
            conn.execute("UPDATE workspaces SET is_active=0")
            conn.execute("UPDATE workspaces SET is_active=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(ws['id']),))
        return get_workspace(self, int(ws['id']))

    def clear_active_workspace(self):
        with self._lock, self._connection() as conn:
            conn.execute("UPDATE workspaces SET is_active=0")

    def update_workspace_metadata(self, workspace_id: int, metadata: dict, kind: str | None = None):
        with self._lock, self._connection() as conn:
            if kind:
                conn.execute("UPDATE workspaces SET metadata_json=?,kind=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (json.dumps(metadata, ensure_ascii=False), kind, int(workspace_id)))
            else:
                conn.execute("UPDATE workspaces SET metadata_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (json.dumps(metadata, ensure_ascii=False), int(workspace_id)))

    def create_task(self, goal: str, plan: dict[str, Any], status: str = 'planned'):
        active = active_workspace(self)
        if not active:
            return original_create_task(self, goal, plan, status)
        with self._lock, self._connection() as conn:
            cur = conn.execute("INSERT INTO tasks(goal,status,plan_json,workspace_id) VALUES (?,?,?,?)",
                               (goal.strip(), status, json.dumps(plan, ensure_ascii=False), int(active['id'])))
            return int(cur.lastrowid)

    def attach_task_workspace(self, item):
        if not item:
            return item
        wid = item.get('workspace_id')
        item['workspace'] = get_workspace(self, int(wid)) if wid else None
        item['workspace_name'] = item['workspace'].get('name') if item.get('workspace') else None
        return item

    def list_tasks(self, limit: int = 12):
        limit = max(1, min(int(limit), 50))
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT id,goal,status,summary,workspace_id,created_at,updated_at FROM tasks ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [attach_task_workspace(self, dict(r)) for r in rows]

    def get_task(self, task_id: int):
        data = original_get_task(self, task_id)
        if not data:
            return None
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT workspace_id FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        data['workspace_id'] = row['workspace_id'] if row else None
        return attach_task_workspace(self, data)

    def stats(self):
        with self._lock, self._connection() as conn:
            counts = {name: int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()['n'])
                      for name in ('messages', 'memory_items', 'workspaces', 'tasks')}
        try:
            size = round(self.db_path.stat().st_size / 1024 / 1024, 2)
        except OSError:
            size = 0.0
        return {'db_size_mb': size, **counts}

    MemoryStore.__init__ = init
    MemoryStore.set_memory = set_memory
    MemoryStore.recent_memory_items = recent_memory_items
    MemoryStore.search_memory = search_memory
    MemoryStore.create_workspace = create_workspace
    MemoryStore.get_workspace = get_workspace
    MemoryStore.list_workspaces = list_workspaces
    MemoryStore.active_workspace = active_workspace
    MemoryStore.resolve_workspace = resolve_workspace
    MemoryStore.set_active_workspace = set_active_workspace
    MemoryStore.clear_active_workspace = clear_active_workspace
    MemoryStore.update_workspace_metadata = update_workspace_metadata
    MemoryStore.create_task = create_task
    MemoryStore.list_tasks = list_tasks
    MemoryStore.get_task = get_task
    MemoryStore.stats = stats
    MemoryStore._nova_v060_patched = True
    return MemoryStore
