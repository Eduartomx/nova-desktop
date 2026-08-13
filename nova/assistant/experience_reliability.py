from __future__ import annotations

"""Experience & Reliability Loop para Skills de Nova.

Mantiene métricas locales de comportamiento por versión de Skill. No persiste
prompts, argumentos, outputs ni contenido del playbook: solo IDs, versión,
resultado y timestamps. Una Skill nunca se edita o deshabilita automáticamente;
únicamente puede degradarse su trust_level `verified` -> `draft` cuando evidencia
reciente demuestra fallos, para obligar a volver a verificarla.
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


DEFAULT_RELIABILITY_CONFIG: dict[str, Any] = {
    "enabled": True,
    "rolling_window": 12,
    "minimum_runs": 3,
    "stable_threshold": 0.78,
    "degraded_threshold": 0.55,
    "consecutive_failures_review": 2,
    "stale_days": 60,
    "auto_demote_verified": True,
    "max_events": 2500,
    "inject_context": True,
    "max_prompt_items": 3,
}

_FINAL_OUTCOMES = {"success", "failure"}
_BANDS = {"unproven", "learning", "stable", "watch", "degraded", "stale"}


def _merge_config(config: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_RELIABILITY_CONFIG)
    if isinstance(config, dict):
        out.update(config)
    return out


class SkillReliability:
    def __init__(self, config: dict[str, Any] | None = None, registry=None, db_path: Path | None = None):
        self.config = _merge_config(config)
        self.registry = registry
        root = Path(__file__).resolve().parent.parent
        self.db_path = Path(db_path or (root / "data" / "skill_reliability.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def configure(self, config: dict[str, Any] | None = None):
        self.config = _merge_config(config)
        return self

    def attach_registry(self, registry=None):
        if registry is not None:
            self.registry = registry
            try:
                registry._reliability_engine = self
            except Exception:
                pass
        return self

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skill_reliability_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id INTEGER NOT NULL,
                    run_id INTEGER NOT NULL,
                    skill_version INTEGER NOT NULL DEFAULT 1,
                    workspace_id INTEGER,
                    outcome TEXT NOT NULL DEFAULT 'prepared',
                    source TEXT NOT NULL DEFAULT 'skill_run',
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    UNIQUE(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_rel_events_skill
                    ON skill_reliability_events(skill_id, skill_version, updated_ts);

                CREATE TABLE IF NOT EXISTS skill_reliability_state (
                    skill_id INTEGER PRIMARY KEY,
                    skill_version INTEGER NOT NULL DEFAULT 1,
                    score REAL NOT NULL DEFAULT 0.5,
                    band TEXT NOT NULL DEFAULT 'unproven',
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_outcome TEXT NOT NULL DEFAULT '',
                    last_success_ts REAL NOT NULL DEFAULT 0,
                    last_failure_ts REAL NOT NULL DEFAULT 0,
                    last_run_ts REAL NOT NULL DEFAULT 0,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    demoted INTEGER NOT NULL DEFAULT 0,
                    updated_ts REAL NOT NULL
                );
                """
            )

    def _trim(self):
        max_rows = max(200, int(self.config.get("max_events", 2500)))
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM skill_reliability_events WHERE id NOT IN "
                "(SELECT id FROM skill_reliability_events ORDER BY id DESC LIMIT ?)",
                (max_rows,),
            )
            conn.commit()

    def observe_prepared(self, run_id: int, skill: dict[str, Any], workspace_id: int | None = None) -> None:
        if not self.enabled or not skill:
            return
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO skill_reliability_events(
                    skill_id,run_id,skill_version,workspace_id,outcome,source,created_ts,updated_ts
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    skill_id=excluded.skill_id, skill_version=excluded.skill_version,
                    workspace_id=excluded.workspace_id, updated_ts=excluded.updated_ts""",
                (
                    int(skill.get("id")), int(run_id), int(skill.get("version", 1) or 1),
                    int(workspace_id) if workspace_id is not None else None,
                    "prepared", "skill_run", now, now,
                ),
            )
            conn.commit()
        self._trim()
        self.refresh(int(skill.get("id")))

    def observe_finished(self, run: dict[str, Any] | None, source: str = "skill_finish") -> dict[str, Any]:
        if not self.enabled or not run:
            return {}
        status = str(run.get("status") or "")
        outcome = "success" if status == "completed" else "failure" if status == "failed" else "returned"
        run_id = int(run.get("id"))
        skill_id = int(run.get("skill_id"))
        now = time.time()
        skill = self.registry.get(skill_id) if self.registry is not None else None
        version = int((skill or {}).get("version", 1) or 1)
        workspace_id = run.get("workspace_id")
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT skill_version,created_ts FROM skill_reliability_events WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                version = int(existing["skill_version"] or version)
                created = float(existing["created_ts"] or now)
            else:
                created = now
            conn.execute(
                """INSERT INTO skill_reliability_events(
                    skill_id,run_id,skill_version,workspace_id,outcome,source,created_ts,updated_ts
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    outcome=excluded.outcome, source=excluded.source, updated_ts=excluded.updated_ts""",
                (
                    skill_id, run_id, version,
                    int(workspace_id) if workspace_id is not None else None,
                    outcome, str(source or "skill_finish")[:60], created, now,
                ),
            )
            conn.commit()
        self._trim()
        return self.refresh(skill_id)

    def observe_saved(self, skill: dict[str, Any] | None) -> dict[str, Any]:
        if not self.enabled or not skill:
            return {}
        return self.refresh(int(skill.get("id")))

    def _current_skill(self, skill_id: int) -> dict[str, Any] | None:
        if self.registry is None:
            return None
        try:
            return self.registry.get(int(skill_id))
        except Exception:
            return None

    def _apply_trust_policy(self, skill: dict[str, Any] | None, state: dict[str, Any]) -> bool:
        if not skill or not self.config.get("auto_demote_verified", True):
            return False
        if str(skill.get("trust_level") or "") != "verified":
            return False
        # No degradamos una Skill solo porque todavía está aprendiendo. Debe existir
        # evidencia negativa reciente o estar obsoleta por inactividad prolongada.
        should_demote = (
            state.get("band") in {"degraded", "stale"}
            or (state.get("band") == "watch" and int(state.get("failures", 0)) > 0)
        )
        if not should_demote or self.registry is None:
            return False
        try:
            with self.registry._lock, self.registry._connect() as conn:
                conn.execute(
                    "UPDATE skills SET trust_level='draft',updated_at=CURRENT_TIMESTAMP WHERE id=? AND trust_level='verified'",
                    (int(skill.get("id")),),
                )
                changed = conn.total_changes > 0
                conn.commit()
            return bool(changed)
        except Exception:
            return False

    def refresh(self, skill_id: int) -> dict[str, Any]:
        skill = self._current_skill(int(skill_id))
        if not skill:
            return {}
        version = int(skill.get("version", 1) or 1)
        window = max(3, min(int(self.config.get("rolling_window", 12)), 50))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT outcome,updated_ts FROM skill_reliability_events
                   WHERE skill_id=? AND skill_version=? AND outcome IN ('success','failure')
                   ORDER BY updated_ts DESC LIMIT ?""",
                (int(skill_id), version, window),
            ).fetchall()
        outcomes = [str(row["outcome"]) for row in rows]
        successes = outcomes.count("success")
        failures = outcomes.count("failure")
        total = successes + failures
        consecutive_failures = 0
        for outcome in outcomes:
            if outcome == "failure":
                consecutive_failures += 1
            else:
                break
        now = time.time()
        last_run_ts = float(rows[0]["updated_ts"] or 0) if rows else 0.0
        last_success_ts = next((float(row["updated_ts"]) for row in rows if row["outcome"] == "success"), 0.0)
        last_failure_ts = next((float(row["updated_ts"]) for row in rows if row["outcome"] == "failure"), 0.0)
        last_outcome = outcomes[0] if outcomes else ""
        # Prior beta suave: evita declarar una Skill perfecta tras una sola ejecución.
        score = (successes + 2.0) / (total + 3.0) if total else 0.5
        minimum = max(1, int(self.config.get("minimum_runs", 3)))
        stable_threshold = float(self.config.get("stable_threshold", 0.78))
        degraded_threshold = float(self.config.get("degraded_threshold", 0.55))
        stale_seconds = max(1, int(self.config.get("stale_days", 60))) * 86400
        stale = bool(last_run_ts and now - last_run_ts >= stale_seconds)
        fail_limit = max(1, int(self.config.get("consecutive_failures_review", 2)))

        reason = ""
        if stale:
            band = "stale"
            reason = "sin_ejecuciones_recientes"
        elif consecutive_failures >= fail_limit:
            band = "degraded"
            reason = "fallos_consecutivos"
        elif total >= max(4, minimum) and score < degraded_threshold:
            band = "degraded"
            reason = "tasa_reciente_baja"
        elif failures > 0 and last_outcome == "failure":
            band = "watch"
            reason = "ultimo_resultado_fallo"
        elif total < minimum:
            band = "learning" if total else "unproven"
            reason = "historial_insuficiente" if total else "sin_ejecuciones_verificadas"
        elif score >= stable_threshold:
            band = "stable"
            reason = "historial_reciente_estable"
        else:
            band = "watch"
            reason = "confianza_reciente_intermedia"
        needs_review = band in {"degraded", "stale"}

        state = {
            "skill_id": int(skill_id),
            "skill_version": version,
            "score": round(float(score), 4),
            "band": band,
            "successes": successes,
            "failures": failures,
            "consecutive_failures": consecutive_failures,
            "last_outcome": last_outcome,
            "last_success_ts": last_success_ts,
            "last_failure_ts": last_failure_ts,
            "last_run_ts": last_run_ts,
            "needs_review": bool(needs_review),
            "reason": reason,
            "demoted": False,
            "updated_ts": now,
        }
        demoted = self._apply_trust_policy(skill, state)
        state["demoted"] = demoted
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO skill_reliability_state(
                    skill_id,skill_version,score,band,successes,failures,consecutive_failures,last_outcome,
                    last_success_ts,last_failure_ts,last_run_ts,needs_review,reason,demoted,updated_ts
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(skill_id) DO UPDATE SET
                    skill_version=excluded.skill_version,score=excluded.score,band=excluded.band,
                    successes=excluded.successes,failures=excluded.failures,
                    consecutive_failures=excluded.consecutive_failures,last_outcome=excluded.last_outcome,
                    last_success_ts=excluded.last_success_ts,last_failure_ts=excluded.last_failure_ts,
                    last_run_ts=excluded.last_run_ts,needs_review=excluded.needs_review,
                    reason=excluded.reason,demoted=excluded.demoted,updated_ts=excluded.updated_ts""",
                (
                    state["skill_id"], state["skill_version"], state["score"], state["band"],
                    state["successes"], state["failures"], state["consecutive_failures"], state["last_outcome"],
                    state["last_success_ts"], state["last_failure_ts"], state["last_run_ts"],
                    1 if state["needs_review"] else 0, state["reason"], 1 if demoted else 0, now,
                ),
            )
            conn.commit()
        state["skill_name"] = str(skill.get("name") or "")
        state["trust_level"] = str((self._current_skill(skill_id) or skill).get("trust_level") or "")
        return state

    def report(self, skill: int | str | dict[str, Any]) -> dict[str, Any]:
        if self.registry is None:
            return {}
        row = skill if isinstance(skill, dict) else self.registry.get(skill)
        if not row:
            return {}
        return self.refresh(int(row.get("id")))

    def review_queue(self, limit: int = 20) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        rows = self.registry.list(include_disabled=True, limit=500)
        states: list[dict[str, Any]] = []
        for skill in rows:
            state = self.refresh(int(skill.get("id")))
            if state.get("needs_review"):
                state["enabled"] = bool(skill.get("enabled"))
                states.append(state)
        states.sort(key=lambda x: (0 if x.get("band") == "degraded" else 1, float(x.get("score", 1.0))))
        return states[: max(1, min(int(limit), 100))]

    def status(self) -> dict[str, Any]:
        queue = self.review_queue(500) if self.registry is not None else []
        with self._connect() as conn:
            events = int(conn.execute("SELECT COUNT(*) FROM skill_reliability_events").fetchone()[0])
            tracked = int(conn.execute("SELECT COUNT(*) FROM skill_reliability_state").fetchone()[0])
        degraded = sum(1 for x in queue if x.get("band") == "degraded")
        stale = sum(1 for x in queue if x.get("band") == "stale")
        return {
            "enabled": self.enabled,
            "tracked_skills": tracked,
            "events": events,
            "needs_review": len(queue),
            "degraded": degraded,
            "stale": stale,
            "rolling_window": int(self.config.get("rolling_window", 12)),
            "stale_days": int(self.config.get("stale_days", 60)),
            "auto_demote_verified": bool(self.config.get("auto_demote_verified", True)),
            "auto_disables_skills": False,
            "auto_edits_playbooks": False,
            "persists_content": False,
            "db_path": str(self.db_path),
        }

    def format_skill_note(self, skill: dict[str, Any]) -> str:
        state = self.report(skill)
        if not state:
            return ""
        band = str(state.get("band") or "unproven")
        score = float(state.get("score", 0.5))
        if band in {"degraded", "stale", "watch"}:
            return (
                "FIABILIDAD DE ESTA SKILL: "
                f"{band} · índice histórico {score:.2f}/1.00 · motivo={state.get('reason')}. "
                "Revalida el procedimiento en el contexto actual antes de confiar en él."
            )
        return f"FIABILIDAD DE ESTA SKILL: {band} · índice histórico {score:.2f}/1.00."

    def compact_context(self) -> str:
        if not self.enabled or not self.config.get("inject_context", True):
            return ""
        rows = self.review_queue(max(1, int(self.config.get("max_prompt_items", 3))))
        if not rows:
            return ""
        lines = ["Skills que requieren revisión antes de reutilizarse:"]
        for row in rows:
            lines.append(
                f"- {row.get('skill_name')} v{row.get('skill_version')} · {row.get('band')} · "
                f"índice {float(row.get('score',0)):.2f} · {row.get('reason')}"
            )
        return "\n".join(lines)


_instances: dict[tuple[str, int], SkillReliability] = {}


def get_skill_reliability(config: dict[str, Any] | None = None, registry=None) -> SkillReliability:
    cfg = config or {}
    root = Path(__file__).resolve().parent.parent
    key = (str(root), id(registry) if registry is not None else 0)
    service = _instances.get(key)
    rel_cfg = cfg.get("skill_reliability", {}) if isinstance(cfg, dict) else {}
    if service is None:
        service = SkillReliability(rel_cfg, registry=registry)
        _instances[key] = service
    else:
        service.configure(rel_cfg).attach_registry(registry)
    if registry is not None:
        service.attach_registry(registry)
    return service


def install_skill_reliability_hooks():
    """Enlaza el motor a SkillRegistry sin convertirlo en parte del playbook."""
    from .skills import SkillRegistry

    if getattr(SkillRegistry, "_nova_reliability_patched", False):
        return SkillRegistry

    original_start = SkillRegistry.start_run
    original_finish = SkillRegistry.finish_run
    original_save = SkillRegistry.save
    original_format = SkillRegistry.format_playbook

    def start_run(self, compiled, workspace_id=None):
        run_id = original_start(self, compiled, workspace_id=workspace_id)
        engine = getattr(self, "_reliability_engine", None)
        if engine is not None:
            try:
                resolved_workspace = workspace_id if workspace_id is not None else self._active_workspace_id()
                engine.observe_prepared(run_id, compiled.skill, resolved_workspace)
            except Exception:
                pass
        return run_id

    def finish_run(self, run_id, success, summary=""):
        row = original_finish(self, run_id, success, summary)
        engine = getattr(self, "_reliability_engine", None)
        if engine is not None and row:
            try:
                engine.observe_finished(row)
            except Exception:
                pass
        return row

    def save(self, *args, **kwargs):
        row = original_save(self, *args, **kwargs)
        engine = getattr(self, "_reliability_engine", None)
        if engine is not None and row:
            try:
                engine.observe_saved(row)
            except Exception:
                pass
        return row

    def format_playbook(self, compiled, run_id=None):
        text = original_format(self, compiled, run_id=run_id)
        engine = getattr(self, "_reliability_engine", None)
        if engine is not None:
            try:
                note = engine.format_skill_note(compiled.skill)
                if note:
                    text += "\n" + note
            except Exception:
                pass
        return text

    SkillRegistry.start_run = start_run
    SkillRegistry.finish_run = finish_run
    SkillRegistry.save = save
    SkillRegistry.format_playbook = format_playbook
    SkillRegistry._nova_reliability_patched = True
    return SkillRegistry
