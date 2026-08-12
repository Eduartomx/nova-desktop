from __future__ import annotations

"""Workspace Auto-Detection para Nova.

Aprende asociaciones locales entre aplicaciones y workspaces a partir de evidencia
confiable acumulada. No usa LLM, no persiste títulos/cwd y no cambia el workspace
activo por defecto. El aprendizaje por título aislado se rechaza para reducir
falsos positivos y envenenamiento por texto externo.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


DEFAULT_WORKSPACE_AUTODETECT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "learn_enabled": True,
    "observe_interval_seconds": 4.0,
    "learn_cooldown_seconds": 20.0,
    "suggestion_threshold": 0.84,
    "ambiguity_margin": 0.18,
    "minimum_confirmations": 3,
    "auto_activate": False,
    "auto_activate_threshold": 0.97,
    "auto_activate_min_confirmations": 6,
    "auto_activate_dwell_seconds": 15.0,
    "auto_activate_cooldown_seconds": 90.0,
    "learn_app_kinds": ["code_editor", "terminal", "game", "office"],
}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


class WorkspaceAutoDetector:
    def __init__(self, perception_engine, memory=None, config: dict[str, Any] | None = None, db_path: Path | None = None):
        self.engine = perception_engine
        self.memory = memory
        self.config = dict(DEFAULT_WORKSPACE_AUTODETECT_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        root = Path(__file__).resolve().parent.parent
        self.db_path = Path(db_path or (root / "data" / "workspace_autodetect.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_learn_at: dict[tuple[str, str, int], float] = {}
        self._dwell_key: tuple[str, int] | None = None
        self._dwell_since = 0.0
        self._last_activation_at = 0.0
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def configure(self, config: dict[str, Any] | None = None):
        merged = dict(DEFAULT_WORKSPACE_AUTODETECT_CONFIG)
        if isinstance(config, dict):
            merged.update(config)
        self.config = merged
        return self

    def attach_memory(self, memory):
        self.memory = memory
        return self

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_app_associations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    process_name TEXT NOT NULL,
                    app_kind TEXT NOT NULL,
                    workspace_id INTEGER NOT NULL,
                    confirmations INTEGER NOT NULL DEFAULT 0,
                    strong_confirmations INTEGER NOT NULL DEFAULT 0,
                    contradictions INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'learned',
                    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(process_name, app_kind, workspace_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_auto_process ON workspace_app_associations(process_name,app_kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_auto_workspace ON workspace_app_associations(workspace_id)")

    def _workspace(self, workspace_id: int | None):
        if workspace_id is None or self.memory is None:
            return None
        try:
            return self.memory.resolve_workspace(int(workspace_id))
        except Exception:
            return None

    def _allowed_kind(self, app_kind: str) -> bool:
        allowed = {str(x).casefold() for x in self.config.get("learn_app_kinds", [])}
        return str(app_kind or "").casefold() in allowed

    @staticmethod
    def _confidence(confirmations: int, strong: int, contradictions: int, pinned: bool = False) -> float:
        if pinned:
            return 1.0
        score = 0.42 + min(0.32, max(0, confirmations) * 0.08) + min(0.20, max(0, strong) * 0.10)
        score -= min(0.42, max(0, contradictions) * 0.12)
        return round(max(0.0, min(0.98, score)), 3)

    def _upsert_evidence(self, process_name: str, app_kind: str, workspace_id: int, *, strong: bool, source: str):
        proc = _norm(process_name)
        kind = _norm(app_kind)
        if not proc or not kind or not self._allowed_kind(kind):
            return None
        now = time.monotonic()
        key = (proc, kind, int(workspace_id))
        cooldown = max(2.0, _float(self.config.get("learn_cooldown_seconds"), 20.0))
        if now - self._last_learn_at.get(key, 0.0) < cooldown:
            return None
        self._last_learn_at[key] = now

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_app_associations WHERE process_name=? AND app_kind=? AND workspace_id=?",
                (proc, kind, int(workspace_id)),
            ).fetchone()
            confirmations = int(row["confirmations"]) + 1 if row else 1
            strong_count = int(row["strong_confirmations"]) + (1 if strong else 0) if row else (1 if strong else 0)
            contradictions = int(row["contradictions"]) if row else 0
            pinned = bool(row["pinned"]) if row else False
            confidence = self._confidence(confirmations, strong_count, contradictions, pinned)
            if row:
                conn.execute(
                    "UPDATE workspace_app_associations SET confirmations=?,strong_confirmations=?,confidence=?,source=?,last_seen=CURRENT_TIMESTAMP WHERE id=?",
                    (confirmations, strong_count, confidence, str(source)[:40], int(row["id"])),
                )
            else:
                conn.execute(
                    "INSERT INTO workspace_app_associations(process_name,app_kind,workspace_id,confirmations,strong_confirmations,confidence,source) VALUES (?,?,?,?,?,?,?)",
                    (proc, kind, int(workspace_id), confirmations, strong_count, confidence, str(source)[:40]),
                )

            # Evidencia fuerte para un workspace contradice asociaciones competidoras
            # del mismo proceso/tipo. Esto evita que una app genérica se quede pegada
            # para siempre a un proyecto antiguo.
            if strong:
                others = conn.execute(
                    "SELECT id,confirmations,strong_confirmations,contradictions,pinned FROM workspace_app_associations WHERE process_name=? AND app_kind=? AND workspace_id<>?",
                    (proc, kind, int(workspace_id)),
                ).fetchall()
                for other in others:
                    if int(other["pinned"]):
                        continue
                    contrad = int(other["contradictions"]) + 1
                    conf = self._confidence(int(other["confirmations"]), int(other["strong_confirmations"]), contrad, False)
                    conn.execute(
                        "UPDATE workspace_app_associations SET contradictions=?,confidence=? WHERE id=?",
                        (contrad, conf, int(other["id"])),
                    )
            conn.commit()
        return self.predict(proc, kind)

    def observe(self, state: dict[str, Any] | None = None):
        if not self.enabled or not self.config.get("learn_enabled", True):
            return {"ok": True, "learned": False, "reason": "disabled"}
        state = dict(state or (self.engine.current(refresh=False) if self.engine is not None else {}))
        external = state.get("external") if isinstance(state.get("external"), dict) else {}
        probable = state.get("probable_workspace") if isinstance(state.get("probable_workspace"), dict) else None
        active = state.get("active_workspace") if isinstance(state.get("active_workspace"), dict) else None
        if not external or not probable:
            return {"ok": True, "learned": False, "reason": "insufficient_context"}

        process_name = str(external.get("process") or "")
        app_kind = str(external.get("app_kind") or "")
        confidence = _float(probable.get("confidence"))
        reason = _norm(probable.get("reason"))
        strong = reason.startswith("cwd dentro") and confidence >= 0.95
        corroborated = bool(active and str(active.get("id")) == str(probable.get("id")) and confidence >= 0.88)

        # Un título de ventana aislado nunca entrena la asociación. Solo se acepta
        # si el workspace activo elegido por el usuario corrobora la coincidencia.
        if not strong and not corroborated:
            return {"ok": True, "learned": False, "reason": "untrusted_or_weak_evidence"}
        if not self._allowed_kind(app_kind):
            return {"ok": True, "learned": False, "reason": "app_kind_not_learned"}

        prediction = self._upsert_evidence(
            process_name,
            app_kind,
            int(probable["id"]),
            strong=strong,
            source="cwd" if strong else "active_workspace_corroboration",
        )
        return {"ok": True, "learned": prediction is not None, "prediction": prediction}

    def associations(self, process_name: str | None = None, app_kind: str | None = None, limit: int = 50):
        sql = "SELECT * FROM workspace_app_associations"
        where: list[str] = []
        values: list[Any] = []
        if process_name:
            where.append("process_name=?")
            values.append(_norm(process_name))
        if app_kind:
            where.append("app_kind=?")
            values.append(_norm(app_kind))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY pinned DESC, confidence DESC, last_seen DESC LIMIT ?"
        values.append(max(1, min(int(limit), 200)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(values)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            ws = self._workspace(int(item["workspace_id"]))
            item["workspace_name"] = ws.get("name") if ws else None
            out.append(item)
        return out

    def predict(self, process_name: str, app_kind: str):
        rows = self.associations(process_name, app_kind, limit=20)
        if not rows:
            return None
        top = rows[0]
        minimum = max(1, int(self.config.get("minimum_confirmations", 3)))
        threshold = _float(self.config.get("suggestion_threshold"), 0.84)
        if not int(top.get("pinned") or 0) and int(top.get("confirmations") or 0) < minimum:
            return None
        if _float(top.get("confidence")) < threshold:
            return None
        second = rows[1] if len(rows) > 1 else None
        margin = _float(top.get("confidence")) - (_float(second.get("confidence")) if second else 0.0)
        if second and not int(top.get("pinned") or 0) and margin < _float(self.config.get("ambiguity_margin"), 0.18):
            return None
        ws = self._workspace(int(top["workspace_id"]))
        if not ws:
            return None
        return {
            "id": int(ws["id"]),
            "name": ws.get("name"),
            "kind": ws.get("kind", "generic"),
            "path": ws.get("path"),
            "confidence": round(_float(top.get("confidence")), 3),
            "reason": "asociación aprendida de aplicación",
            "source": "pinned" if int(top.get("pinned") or 0) else "learned",
            "confirmations": int(top.get("confirmations") or 0),
            "strong_confirmations": int(top.get("strong_confirmations") or 0),
            "ambiguity_margin": round(margin, 3),
            "process": process_name,
            "app_kind": app_kind,
        }

    def suggestion(self, state: dict[str, Any] | None = None, refresh: bool = False):
        state = dict(state or (self.engine.current(refresh=bool(refresh)) if self.engine is not None else {}))
        raw = state.get("probable_workspace") if isinstance(state.get("probable_workspace"), dict) else None
        if raw and _float(raw.get("confidence")) >= 0.78:
            result = dict(raw)
            result["source"] = "live"
            return result
        external = state.get("external") if isinstance(state.get("external"), dict) else {}
        if not external:
            return None
        return self.predict(str(external.get("process") or ""), str(external.get("app_kind") or ""))

    def pin_current_to_workspace(self, workspace_id: int | None = None):
        state = self.engine.current(refresh=True) if self.engine is not None else {}
        external = state.get("external") if isinstance(state.get("external"), dict) else {}
        if not external:
            return {"ok": False, "error": "No hay una aplicación externa observada."}
        if workspace_id is None:
            active = self.memory.active_workspace() if self.memory is not None else None
            workspace_id = int(active["id"]) if active else None
        ws = self._workspace(workspace_id)
        if not ws:
            return {"ok": False, "error": "No hay un workspace válido para asociar."}
        proc = _norm(external.get("process"))
        kind = _norm(external.get("app_kind"))
        if not proc or not kind:
            return {"ok": False, "error": "No pude identificar la aplicación actual."}
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_app_associations WHERE process_name=? AND app_kind=? AND workspace_id=?",
                (proc, kind, int(ws["id"])),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE workspace_app_associations SET pinned=1,confidence=1.0,source='user_pinned',last_seen=CURRENT_TIMESTAMP WHERE id=?",
                    (int(row["id"]),),
                )
            else:
                conn.execute(
                    "INSERT INTO workspace_app_associations(process_name,app_kind,workspace_id,confirmations,strong_confirmations,pinned,confidence,source) VALUES (?,?,?,?,?,?,?,?)",
                    (proc, kind, int(ws["id"]), 1, 0, 1, 1.0, "user_pinned"),
                )
            conn.commit()
        return {"ok": True, "workspace": ws, "process": proc, "app_kind": kind, "confidence": 1.0}

    def forget(self, process_name: str | None = None, app_kind: str | None = None, workspace_id: int | None = None):
        clauses: list[str] = []
        values: list[Any] = []
        if process_name:
            clauses.append("process_name=?")
            values.append(_norm(process_name))
        if app_kind:
            clauses.append("app_kind=?")
            values.append(_norm(app_kind))
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            values.append(int(workspace_id))
        if not clauses:
            return {"ok": False, "error": "Se requiere al menos un filtro para olvidar asociaciones."}
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM workspace_app_associations WHERE " + " AND ".join(clauses), tuple(values))
            deleted = int(cur.rowcount or 0)
            conn.commit()
        return {"ok": True, "deleted": deleted}

    def _maybe_auto_activate(self, state: dict[str, Any], suggestion: dict[str, Any] | None):
        if not self.config.get("auto_activate", False) or not suggestion or self.memory is None:
            self._dwell_key = None
            self._dwell_since = 0.0
            return None
        if suggestion.get("source") not in {"learned", "pinned"}:
            return None
        if _float(suggestion.get("confidence")) < _float(self.config.get("auto_activate_threshold"), 0.97):
            return None
        if suggestion.get("source") != "pinned" and int(suggestion.get("confirmations") or 0) < int(self.config.get("auto_activate_min_confirmations", 6)):
            return None
        active = state.get("active_workspace") if isinstance(state.get("active_workspace"), dict) else None
        if active and int(active.get("id")) == int(suggestion["id"]):
            return None

        external = state.get("external") if isinstance(state.get("external"), dict) else {}
        key = (_norm(external.get("process")), int(suggestion["id"]))
        now = time.monotonic()
        if key != self._dwell_key:
            self._dwell_key = key
            self._dwell_since = now
            return None
        if now - self._dwell_since < _float(self.config.get("auto_activate_dwell_seconds"), 15.0):
            return None
        if now - self._last_activation_at < _float(self.config.get("auto_activate_cooldown_seconds"), 90.0):
            return None
        activated = self.memory.set_active_workspace(int(suggestion["id"]))
        if activated:
            self._last_activation_at = now
            return activated
        return None

    def sample_once(self):
        if not self.enabled:
            return {"ok": True, "enabled": False}
        state = self.engine.current(refresh=False) if self.engine is not None else {}
        learned = self.observe(state)
        suggestion = self.suggestion(state)
        activated = self._maybe_auto_activate(state, suggestion)
        return {"ok": True, "enabled": True, "learn": learned, "suggestion": suggestion, "activated": activated}

    def status(self, refresh: bool = False):
        state = self.engine.current(refresh=bool(refresh)) if self.engine is not None else {}
        suggestion = self.suggestion(state)
        rows = self.associations(limit=200)
        active = state.get("active_workspace") if isinstance(state.get("active_workspace"), dict) else None
        return {
            "ok": True,
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "learn_enabled": bool(self.config.get("learn_enabled", True)),
            "auto_activate": bool(self.config.get("auto_activate", False)),
            "associations": len(rows),
            "pinned_associations": sum(1 for row in rows if int(row.get("pinned") or 0)),
            "suggestion": suggestion,
            "active_workspace": active,
            "stores_titles": False,
            "stores_cwd": False,
            "uses_llm": False,
        }

    def format_suggestion(self, refresh: bool = True) -> str:
        state = self.engine.current(refresh=bool(refresh)) if self.engine is not None else {}
        suggestion = self.suggestion(state)
        active = state.get("active_workspace") if isinstance(state.get("active_workspace"), dict) else None
        if not suggestion:
            return "Todavía no tengo evidencia suficiente para asociar la aplicación actual con un proyecto concreto."
        source = str(suggestion.get("source") or "live")
        text = f"Proyecto probable: {suggestion.get('name')} ({_float(suggestion.get('confidence'))*100:.0f}% de confianza, {source})."
        if active:
            text += f" Workspace activo: {active.get('name')}."
        if source in {"learned", "pinned"}:
            text += f" Evidencia acumulada: {int(suggestion.get('confirmations') or 0)} observaciones."
        if active and int(active.get("id")) != int(suggestion.get("id")):
            text += " No cambié el workspace automáticamente."
        return text

    def start(self):
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return self
        self._stop.clear()

        def loop():
            interval = max(1.5, _float(self.config.get("observe_interval_seconds"), 4.0))
            while not self._stop.wait(interval):
                try:
                    self.sample_once()
                except Exception:
                    pass

        self._thread = threading.Thread(target=loop, daemon=True, name="nova-workspace-autodetect")
        self._thread.start()
        return self

    def stop(self, timeout: float = 0.4):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        return self


_instances: dict[int, WorkspaceAutoDetector] = {}


def get_workspace_autodetector(config: dict[str, Any] | None = None, memory=None) -> WorkspaceAutoDetector:
    from .perception import get_perception

    engine = get_perception(config or {}, memory)
    key = id(engine)
    cfg = (config or {}).get("workspace_autodetect", {}) if isinstance(config, dict) else {}
    instance = _instances.get(key)
    if instance is None:
        instance = WorkspaceAutoDetector(engine, memory=memory, config=cfg)
        _instances[key] = instance
    else:
        instance.attach_memory(memory)
        instance.configure(cfg)
    return instance
