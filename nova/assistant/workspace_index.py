from __future__ import annotations

import fnmatch
import hashlib
import re
import time
from pathlib import Path
from typing import Any


DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", "venv", "env", "node_modules",
    "build", "dist", "target", "Library", "Temp", "Logs", "obj", "bin",
}
IMPORTANT_NAMES = {
    "server.properties", "eula.txt", "pyproject.toml", "requirements.txt",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "project.godot", "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle",
    "settings.gradle", "gradle.properties", "docker-compose.yml", "compose.yml",
    ".env.example", "README.md", "CHANGELOG.md",
}
IMPORTANT_EXTS = {
    ".py", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".properties", ".xml", ".gradle", ".js", ".ts", ".tsx", ".jsx", ".java",
    ".cs", ".cpp", ".c", ".h", ".hpp", ".ino", ".gd", ".lua", ".md", ".txt",
}


def _sha256(path: Path, limit_bytes: int) -> str | None:
    try:
        if path.stat().st_size > limit_bytes:
            return None
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w.-]+", (text or "").casefold()) if len(t) > 1]


class WorkspaceIndexer:
    """Incremental local index for a registered Nova workspace."""

    def __init__(self, memory, config: dict[str, Any] | None = None):
        self.memory = memory
        self.config = config or {}
        self._migrate()

    def _migrate(self) -> None:
        with self.memory._lock, self.memory._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workspace_files (
                    workspace_id INTEGER NOT NULL,
                    rel_path TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    file_kind TEXT NOT NULL DEFAULT 'file',
                    sha256 TEXT,
                    indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(workspace_id, rel_path),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workspace_index_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    scanned INTEGER NOT NULL DEFAULT 0,
                    added INTEGER NOT NULL DEFAULT 0,
                    modified INTEGER NOT NULL DEFAULT 0,
                    removed INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    finished_at DATETIME,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workspace_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    workspace_id INTEGER NOT NULL,
                    rel_path TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    old_size INTEGER,
                    new_size INTEGER,
                    old_mtime_ns INTEGER,
                    new_mtime_ns INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(run_id) REFERENCES workspace_index_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_changes_workspace ON workspace_changes(workspace_id,id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_files_workspace ON workspace_files(workspace_id,rel_path)")

    def _limits(self) -> tuple[int, int, int]:
        max_files = max(100, min(int(self.config.get("index_max_files", 12000)), 100000))
        max_depth = max(1, min(int(self.config.get("index_max_depth", 8)), 30))
        hash_bytes = max(64 * 1024, min(int(self.config.get("index_hash_max_bytes", 2 * 1024 * 1024)), 64 * 1024 * 1024))
        return max_files, max_depth, hash_bytes

    def _ignored(self, rel: Path) -> bool:
        ignored = set(DEFAULT_IGNORE_DIRS)
        ignored.update(str(x) for x in self.config.get("index_ignore_dirs", []) if x)
        for part in rel.parts[:-1]:
            if part in ignored:
                return True
        patterns = self.config.get("index_ignore_globs", ["*.pyc", "*.tmp", "*.part", "*.log.gz"])
        s = rel.as_posix()
        return any(fnmatch.fnmatch(s, str(p)) for p in patterns)

    def _should_hash(self, path: Path) -> bool:
        return path.name in IMPORTANT_NAMES or path.suffix.casefold() in IMPORTANT_EXTS

    def _walk(self, root: Path):
        max_files, max_depth, hash_bytes = self._limits()
        count = 0
        stack = [(root, 0)]
        while stack and count < max_files:
            folder, depth = stack.pop()
            try:
                children = list(folder.iterdir())
            except OSError:
                continue
            children.sort(key=lambda p: p.name.casefold(), reverse=True)
            for child in children:
                try:
                    rel = child.relative_to(root)
                except ValueError:
                    continue
                if self._ignored(rel):
                    continue
                try:
                    if child.is_dir():
                        if depth < max_depth and child.name not in DEFAULT_IGNORE_DIRS:
                            stack.append((child, depth + 1))
                        continue
                    if not child.is_file():
                        continue
                    st = child.stat()
                except OSError:
                    continue
                digest = _sha256(child, hash_bytes) if self._should_hash(child) else None
                yield {
                    "rel_path": rel.as_posix(),
                    "size": int(st.st_size),
                    "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
                    "file_kind": child.suffix.casefold().lstrip(".") or "file",
                    "sha256": digest,
                }
                count += 1
                if count >= max_files:
                    break

    def index(self, workspace: dict[str, Any], force: bool = False) -> dict[str, Any]:
        if not workspace:
            return {"ok": False, "error": "No hay workspace."}
        wid = int(workspace["id"])
        root = Path(str(workspace["path"]))
        if not root.is_dir():
            return {"ok": False, "error": f"La carpeta no existe: {root}"}

        started = time.perf_counter()
        with self.memory._lock, self.memory._connection() as conn:
            cur = conn.execute("INSERT INTO workspace_index_runs(workspace_id) VALUES (?)", (wid,))
            run_id = int(cur.lastrowid)
            old_rows = conn.execute(
                "SELECT rel_path,size,mtime_ns,file_kind,sha256 FROM workspace_files WHERE workspace_id=?",
                (wid,),
            ).fetchall()
        old = {r["rel_path"]: dict(r) for r in old_rows}

        current: dict[str, dict[str, Any]] = {}
        for row in self._walk(root):
            current[row["rel_path"]] = row

        added, modified, removed = [], [], []
        for path, row in current.items():
            prev = old.get(path)
            if prev is None:
                added.append((path, None, row))
                continue
            changed = force or int(prev["size"]) != row["size"] or int(prev["mtime_ns"]) != row["mtime_ns"]
            if changed and prev.get("sha256") and row.get("sha256") and prev["sha256"] == row["sha256"]:
                changed = False
            if changed:
                modified.append((path, prev, row))
        for path, prev in old.items():
            if path not in current:
                removed.append((path, prev, None))

        duration_ms = int((time.perf_counter() - started) * 1000)
        with self.memory._lock, self.memory._connection() as conn:
            conn.execute("DELETE FROM workspace_files WHERE workspace_id=?", (wid,))
            conn.executemany(
                """INSERT INTO workspace_files(workspace_id,rel_path,size,mtime_ns,file_kind,sha256)
                   VALUES (?,?,?,?,?,?)""",
                [(wid, r["rel_path"], r["size"], r["mtime_ns"], r["file_kind"], r["sha256"]) for r in current.values()],
            )
            changes = [
                (run_id, wid, p, "added", None, n["size"], None, n["mtime_ns"]) for p, _, n in added
            ] + [
                (run_id, wid, p, "modified", o["size"], n["size"], o["mtime_ns"], n["mtime_ns"]) for p, o, n in modified
            ] + [
                (run_id, wid, p, "removed", o["size"], None, o["mtime_ns"], None) for p, o, _ in removed
            ]
            if changes:
                conn.executemany(
                    """INSERT INTO workspace_changes(
                       run_id,workspace_id,rel_path,change_type,old_size,new_size,old_mtime_ns,new_mtime_ns)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    changes,
                )
            conn.execute(
                """UPDATE workspace_index_runs SET status='ok',scanned=?,added=?,modified=?,removed=?,
                   duration_ms=?,finished_at=CURRENT_TIMESTAMP WHERE id=?""",
                (len(current), len(added), len(modified), len(removed), duration_ms, run_id),
            )

        return {
            "ok": True,
            "workspace": workspace.get("name"),
            "workspace_id": wid,
            "run_id": run_id,
            "scanned": len(current),
            "added": len(added),
            "modified": len(modified),
            "removed": len(removed),
            "duration_ms": duration_ms,
            "truncated": len(current) >= self._limits()[0],
        }

    def changes(self, workspace_id: int, limit: int = 80, run_id: int | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.memory._lock, self.memory._connection() as conn:
            if run_id is None:
                row = conn.execute(
                    "SELECT id FROM workspace_index_runs WHERE workspace_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                    (int(workspace_id),),
                ).fetchone()
                if not row:
                    return []
                run_id = int(row["id"])
            rows = conn.execute(
                """SELECT rel_path,change_type,old_size,new_size,created_at
                   FROM workspace_changes WHERE workspace_id=? AND run_id=?
                   ORDER BY id LIMIT ?""",
                (int(workspace_id), int(run_id), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, workspace_id: int, query: str, limit: int = 30) -> list[dict[str, Any]]:
        terms = _tokens(query)
        limit = max(1, min(int(limit), 100))
        with self.memory._lock, self.memory._connection() as conn:
            rows = conn.execute(
                "SELECT rel_path,size,mtime_ns,file_kind,sha256 FROM workspace_files WHERE workspace_id=?",
                (int(workspace_id),),
            ).fetchall()

        scored = []
        for row in rows:
            item = dict(row)
            path = item["rel_path"].casefold()
            name = Path(item["rel_path"]).name.casefold()
            score = 0.0
            for term in terms:
                if term == name:
                    score += 8
                elif term in name:
                    score += 5
                elif term in path:
                    score += 2
            if not terms:
                score = 1
            if score > 0:
                item["score"] = score
                scored.append(item)
        scored.sort(key=lambda x: (-x["score"], x["rel_path"].casefold()))
        return scored[:limit]

    def status(self, workspace_id: int) -> dict[str, Any]:
        with self.memory._lock, self.memory._connection() as conn:
            run = conn.execute(
                """SELECT id,status,scanned,added,modified,removed,duration_ms,error,created_at,finished_at
                   FROM workspace_index_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 1""",
                (int(workspace_id),),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM workspace_files WHERE workspace_id=?", (int(workspace_id),)
            ).fetchone()["n"]
        return {"indexed_files": int(count), "last_run": dict(run) if run else None}
