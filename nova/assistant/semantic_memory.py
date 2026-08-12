from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable


DEFAULT_SEMANTIC_CONFIG: dict[str, Any] = {
    "enabled": True,
    "model": "qwen3-embedding:0.6b",
    "auto_pull_model": False,
    "request_timeout_seconds": 8,
    "batch_size": 16,
    "lazy_index": True,
    "lazy_index_limit": 24,
    "semantic_weight": 0.58,
    "lexical_weight": 0.27,
    "importance_weight": 0.10,
    "recency_weight": 0.05,
    "minimum_semantic_score": 0.12,
}


def _model_base(name: str) -> str:
    return str(name or "").strip().casefold().split(":", 1)[0]


def _memory_text(row: dict[str, Any]) -> str:
    category = str(row.get("category") or "fact").strip()
    key = str(row.get("key") or "").strip()
    value = str(row.get("value") or "").strip()
    return f"Tipo: {category}\nClave: {key}\nContenido: {value}"


def _fingerprint(row: dict[str, Any]) -> str:
    raw = (
        f"{row.get('id','')}\0{row.get('key','')}\0{row.get('value','')}\0"
        f"{row.get('category','')}\0{row.get('workspace_id','')}"
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def _recency_score(value: Any) -> float:
    if not value:
        return 0.5
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400.0)
        return max(0.0, min(1.0, math.exp(-age_days / 120.0)))
    except Exception:
        return 0.5


class SemanticMemoryEngine:
    """Búsqueda híbrida local sobre memory_items usando embeddings de Ollama.

    La base léxica siempre sigue disponible. Si Ollama o el modelo de embeddings
    no están listos, la consulta vuelve inmediatamente al ranking léxico.
    """

    def __init__(self, memory, config: dict[str, Any] | None = None, ollama_host: str = "http://127.0.0.1:11434"):
        self.memory = memory
        self.config: dict[str, Any] = dict(DEFAULT_SEMANTIC_CONFIG)
        self.ollama_host = str(ollama_host or "http://127.0.0.1:11434").rstrip("/")
        self._availability_cache: tuple[float, bool, str] | None = None
        self._last_error = ""
        self.configure(config or {}, ollama_host)
        self.ensure_schema()

    def configure(self, config: dict[str, Any] | None = None, ollama_host: str | None = None):
        merged = dict(DEFAULT_SEMANTIC_CONFIG)
        if isinstance(config, dict):
            merged.update(config)
        self.config = merged
        if ollama_host:
            self.ollama_host = str(ollama_host).rstrip("/")
        self._availability_cache = None
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def model(self) -> str:
        return str(self.config.get("model") or DEFAULT_SEMANTIC_CONFIG["model"])

    def ensure_schema(self):
        with self.memory._lock, self.memory._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model ON memory_embeddings(model)")

    def _json_request(self, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.ollama_host + path,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Nova-Semantic-Memory/0.6.3"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout or float(self.config.get("request_timeout_seconds", 8))) as response:
            return json.load(response)

    def model_available(self, refresh: bool = False) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Semantic Memory está desactivada en config."
        now = time.monotonic()
        if not refresh and self._availability_cache and now - self._availability_cache[0] < 30:
            return self._availability_cache[1], self._availability_cache[2]
        try:
            data = self._json_request("/api/tags", timeout=2.5)
            names = [str(x.get("name") or x.get("model") or "") for x in data.get("models", [])]
            wanted = _model_base(self.model)
            ok = any(_model_base(name) == wanted for name in names)
            detail = f"{self.model} disponible" if ok else f"Falta {self.model}"
            self._availability_cache = (now, ok, detail)
            if ok:
                self._last_error = ""
            return ok, detail
        except Exception as exc:
            self._last_error = str(exc)
            detail = f"Ollama no disponible para embeddings: {exc}"
            self._availability_cache = (now, False, detail)
            return False, detail

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self.model, "input": texts, "truncate": True}
        data = self._json_request("/api/embed", payload=payload)
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise RuntimeError(f"Ollama devolvió {len(vectors)} embeddings para {len(texts)} textos")
        return [[float(v) for v in row] for row in vectors]

    def invalidate(self, memory_id: int):
        with self.memory._lock, self.memory._connection() as conn:
            conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (int(memory_id),))

    def invalidate_by_key(self, key: str, workspace_id: int | None = None):
        with self.memory._lock, self.memory._connection() as conn:
            row = conn.execute(
                "SELECT id FROM memory_items WHERE key=? AND ((workspace_id IS NULL AND ? IS NULL) OR workspace_id=?) LIMIT 1",
                (str(key).strip(), workspace_id, workspace_id),
            ).fetchone()
            if row:
                conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (int(row["id"]),))

    def _candidate_rows(self, workspace_id: int | None = None, limit: int = 300) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.memory._lock, self.memory._connection() as conn:
            if workspace_id is None:
                rows = conn.execute(
                    "SELECT * FROM memory_items WHERE workspace_id IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM memory_items
                    WHERE workspace_id IS NULL OR workspace_id=?
                    ORDER BY (workspace_id IS NOT NULL) DESC, importance DESC, updated_at DESC LIMIT ?
                    """,
                    (int(workspace_id), limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def _stored_embeddings(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self.memory._lock, self.memory._connection() as conn:
            rows = conn.execute(
                f"SELECT memory_id,model,fingerprint,dimensions,vector_json,updated_at FROM memory_embeddings WHERE memory_id IN ({marks})",
                tuple(ids),
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            try:
                item["vector"] = [float(x) for x in json.loads(item.pop("vector_json"))]
            except Exception:
                item["vector"] = []
            result[int(item["memory_id"])] = item
        return result

    def _index_rows(self, rows: list[dict[str, Any]], force: bool = False) -> dict[str, Any]:
        available, detail = self.model_available()
        if not available:
            return {"ok": False, "indexed": 0, "skipped": len(rows), "detail": detail}

        stored = self._stored_embeddings([int(row["id"]) for row in rows])
        pending: list[dict[str, Any]] = []
        for row in rows:
            old = stored.get(int(row["id"]))
            fp = _fingerprint(row)
            if force or not old or old.get("model") != self.model or old.get("fingerprint") != fp:
                pending.append(row)

        if not pending:
            return {"ok": True, "indexed": 0, "skipped": len(rows), "detail": "Índice semántico al día"}

        batch_size = max(1, min(int(self.config.get("batch_size", 16)), 64))
        indexed = 0
        try:
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                vectors = self.embed([_memory_text(row) for row in batch])
                with self.memory._lock, self.memory._connection() as conn:
                    for row, vector in zip(batch, vectors):
                        conn.execute(
                            """
                            INSERT INTO memory_embeddings(memory_id,model,fingerprint,dimensions,vector_json,updated_at)
                            VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
                            ON CONFLICT(memory_id) DO UPDATE SET
                              model=excluded.model,
                              fingerprint=excluded.fingerprint,
                              dimensions=excluded.dimensions,
                              vector_json=excluded.vector_json,
                              updated_at=CURRENT_TIMESTAMP
                            """,
                            (
                                int(row["id"]),
                                self.model,
                                _fingerprint(row),
                                len(vector),
                                json.dumps(vector, separators=(",", ":")),
                            ),
                        )
                indexed += len(batch)
            self._last_error = ""
            return {"ok": True, "indexed": indexed, "skipped": len(rows) - indexed, "detail": f"{indexed} memorias indexadas"}
        except Exception as exc:
            self._last_error = str(exc)
            return {"ok": False, "indexed": indexed, "skipped": len(rows) - indexed, "detail": str(exc)}

    def reindex(self, workspace_id: int | None = None, force: bool = False, limit: int = 1000) -> dict[str, Any]:
        rows = self._candidate_rows(workspace_id, limit=limit)
        result = self._index_rows(rows, force=force)
        result.update({"model": self.model, "candidates": len(rows), "workspace_id": workspace_id})
        return result

    def status(self, workspace_id: int | None = None, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        rows = self._candidate_rows(workspace_id, limit=1000)
        ids = [int(row["id"]) for row in rows]
        stored = self._stored_embeddings(ids)
        valid = 0
        dims: set[int] = set()
        for row in rows:
            item = stored.get(int(row["id"]))
            if item and item.get("model") == self.model and item.get("fingerprint") == _fingerprint(row) and item.get("vector"):
                valid += 1
                dims.add(int(item.get("dimensions") or len(item.get("vector") or [])))
        available, detail = self.model_available(refresh=refresh)
        return {
            "enabled": self.enabled,
            "model": self.model,
            "model_available": available,
            "detail": detail,
            "indexed": valid,
            "pending": max(0, len(rows) - valid),
            "total_candidates": len(rows),
            "dimensions": sorted(dims),
            "last_error": self._last_error,
            "auto_pull_model": bool(self.config.get("auto_pull_model", False)),
            "install_command": f"ollama pull {self.model}",
        }

    def search(
        self,
        query: str,
        limit: int,
        workspace_id: int | None,
        lexical_search: Callable[[str, int, int | None], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 40))
        lexical = lexical_search(query, max(limit * 4, 20), workspace_id)
        for item in lexical:
            item.setdefault("retrieval", "lexical")

        if not self.enabled or not str(query or "").strip():
            return lexical[:limit]

        available, _ = self.model_available()
        if not available:
            return lexical[:limit]

        candidates = self._candidate_rows(workspace_id, limit=300)
        stored = self._stored_embeddings([int(row["id"]) for row in candidates])
        valid_ids = {
            int(row["id"])
            for row in candidates
            if (int(row["id"]) in stored
                and stored[int(row["id"])].get("model") == self.model
                and stored[int(row["id"])].get("fingerprint") == _fingerprint(row)
                and stored[int(row["id"])].get("vector"))
        }

        if bool(self.config.get("lazy_index", True)) and len(valid_ids) < len(candidates):
            lazy_limit = max(1, min(int(self.config.get("lazy_index_limit", 24)), 100))
            pending = [row for row in candidates if int(row["id"]) not in valid_ids][:lazy_limit]
            if pending:
                self._index_rows(pending)
                stored = self._stored_embeddings([int(row["id"]) for row in candidates])

        try:
            query_vector = self.embed([str(query)])[0]
        except Exception as exc:
            self._last_error = str(exc)
            return lexical[:limit]

        lexical_by_id: dict[int, float] = {}
        for item in lexical:
            if item.get("id") is not None:
                lexical_by_id[int(item["id"])] = min(1.0, max(0.0, float(item.get("score") or 0.0) / 8.0))

        sw = float(self.config.get("semantic_weight", 0.58))
        lw = float(self.config.get("lexical_weight", 0.27))
        iw = float(self.config.get("importance_weight", 0.10))
        rw = float(self.config.get("recency_weight", 0.05))
        min_sem = float(self.config.get("minimum_semantic_score", 0.12))
        scored: list[dict[str, Any]] = []

        for row in candidates:
            memory_id = int(row["id"])
            emb = stored.get(memory_id)
            semantic = _cosine(query_vector, emb.get("vector", [])) if emb and emb.get("model") == self.model else 0.0
            lexical_score = lexical_by_id.get(memory_id, 0.0)
            if semantic < min_sem and lexical_score <= 0:
                continue
            importance = max(0.0, min(1.0, float(row.get("importance") or 0.5)))
            recency = _recency_score(row.get("updated_at"))
            workspace_bonus = 0.04 if workspace_id is not None and row.get("workspace_id") == workspace_id else 0.0
            hybrid = sw * max(0.0, semantic) + lw * lexical_score + iw * importance + rw * recency + workspace_bonus
            item = dict(row)
            item.update(
                {
                    "score": round(hybrid, 4),
                    "semantic_score": round(semantic, 4),
                    "lexical_score": round(lexical_score, 4),
                    "retrieval": "hybrid",
                }
            )
            scored.append(item)

        if not scored:
            return lexical[:limit]
        scored.sort(key=lambda x: (float(x.get("score") or 0), str(x.get("updated_at") or "")), reverse=True)
        return scored[:limit]
