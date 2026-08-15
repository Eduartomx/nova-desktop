from __future__ import annotations

"""Confidence Engine determinista para Nova.

El score es una heurística de respaldo/evidencia, NO una probabilidad calibrada de
que una respuesta sea correcta. Nunca usa la confianza declarada por el LLM como
señal. Solo considera señales estructuradas: herramientas, verificaciones,
fallos/contradicciones, riesgo de la petición y confianza de Skills.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


DEFAULT_CONFIDENCE_CONFIG: dict[str, Any] = {
    "enabled": True,
    "persist_assessments": True,
    "max_assessments": 1800,
    "low_threshold": 0.52,
    "high_threshold": 0.78,
    "escalation_candidate_threshold": 0.50,
    "inject_context": True,
    "surface_low_confidence": True,
    "minimum_evidence_for_high": 2,
}

_READ_CUES = (
    "status", "info", "list", "search", "read", "get", "inspect", "context",
    "perception", "anomaly", "vision", "memory", "workspace", "recent", "doctor",
)
_ACTION_CUES = (
    "write", "save", "delete", "remove", "click", "fill", "type", "press", "run",
    "start", "stop", "restart", "open", "close", "set_", "install", "repair", "execute",
)
_VERIFY_CUES = ("verify", "check", "validate", "health", "status", "doctor")


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def classify_request(text: str) -> tuple[str, str]:
    """Devuelve (request_kind, risk_level) sin conservar el texto."""
    t = _norm(text)
    critical = (
        "paga", "pagar", "compra", "comprar", "transfer", "contraseña", "password",
        "credencial", "token", "2fa", "autenticación", "autenticacion", "seguridad de cuenta",
    )
    high = (
        "borra", "borrar", "elimina", "eliminar", "desinstala", "desinstalar", "formatea",
        "formatear", "registro de windows", "regedit", "firewall", "instala", "instalar",
    )
    risk = "critical" if any(x in t for x in critical) else "high" if any(x in t for x in high) else "normal"

    if any(x in t for x in ("por qué", "por que", "error", "falla", "crash", "problema", "diagnost", "no funciona")):
        kind = "diagnosis"
    elif any(x in t for x in ("estado", "ahora", "actual", "qué aplicación", "que aplicacion", "cuánto usa", "cuanto usa")):
        kind = "current_state"
    elif any(x in t for x in ("plan", "planea", "cómo harías", "como harias", "estrategia", "roadmap")):
        kind = "planning"
    elif any(x in t for x in ("abre", "cierra", "sube volumen", "baja volumen", "copia", "pega")):
        kind = "simple_control"
    elif any(x in t for x in ("escribe", "redacta", "inventa", "crea una historia", "poema")):
        kind = "creative"
    else:
        kind = "factual"
    return kind, risk


class ConfidenceEngine:
    def __init__(self, config: dict[str, Any] | None = None, memory=None, db_path: Path | None = None):
        self.config = dict(DEFAULT_CONFIDENCE_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        self.memory = memory
        root = Path(__file__).resolve().parent.parent
        self.db_path = Path(db_path or (root / "data" / "confidence.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._last: dict[str, Any] = {}
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def configure(self, config: dict[str, Any] | None = None):
        merged = dict(DEFAULT_CONFIDENCE_CONFIG)
        if isinstance(config, dict):
            merged.update(config)
        self.config = merged
        return self

    def attach_memory(self, memory=None):
        if memory is not None:
            self.memory = memory
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
                CREATE TABLE IF NOT EXISTS confidence_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_kind TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    score REAL NOT NULL,
                    band TEXT NOT NULL,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    verification_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    contradiction_count INTEGER NOT NULL DEFAULT 0,
                    deterministic INTEGER NOT NULL DEFAULT 0,
                    skill_trust TEXT NOT NULL DEFAULT '',
                    escalation_candidate INTEGER NOT NULL DEFAULT 0,
                    reason_codes_json TEXT NOT NULL DEFAULT '[]',
                    tool_names_json TEXT NOT NULL DEFAULT '[]',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_confidence_created ON confidence_assessments(created_at);
                """
            )

    def begin_request(self, text: str, *, skill_trust: str = "") -> None:
        kind, risk = classify_request(text)
        self._local.session = {
            "request_kind": kind,
            "risk_level": risk,
            "started": time.perf_counter(),
            "tool_success": 0,
            "tool_failure": 0,
            "structured_reads": 0,
            "verification_count": 0,
            "contradiction_count": 0,
            "tool_names": [],
            "action_seen": False,
            "deterministic": False,
            "skill_trust": str(skill_trust or "").casefold(),
            "reason_codes": [],
        }

    def _session(self) -> dict[str, Any] | None:
        return getattr(self._local, "session", None)

    def mark_deterministic(self, reason: str = "deterministic_route") -> None:
        session = self._session()
        if session is not None:
            session["deterministic"] = True
            if reason not in session["reason_codes"]:
                session["reason_codes"].append(str(reason)[:80])

    def mark_skill(self, trust_level: str) -> None:
        session = self._session()
        if session is not None:
            session["skill_trust"] = str(trust_level or "").casefold()

    def record_contradiction(self, reason: str = "contradictory_evidence") -> None:
        session = self._session()
        if session is not None:
            session["contradiction_count"] += 1
            if reason not in session["reason_codes"]:
                session["reason_codes"].append(str(reason)[:80])

    def record_tool(self, tool_name: str, result: Any = None, *, failed: bool = False) -> None:
        session = self._session()
        if session is None:
            return
        name = _norm(tool_name).replace(" ", "_")[:100]
        if name and name not in session["tool_names"]:
            session["tool_names"].append(name)

        if failed:
            ok = False
        elif isinstance(result, dict) and "ok" in result:
            ok = bool(result.get("ok"))
        else:
            ok = result is not None

        if ok:
            session["tool_success"] += 1
        else:
            session["tool_failure"] += 1

        is_read = any(cue in name for cue in _READ_CUES)
        is_action = any(cue in name for cue in _ACTION_CUES)
        is_verify = any(cue in name for cue in _VERIFY_CUES)
        structured = isinstance(result, (dict, list, tuple))

        if ok and structured and is_read:
            session["structured_reads"] += 1
        if ok and is_verify:
            session["verification_count"] += 1
        elif ok and is_read and session.get("action_seen"):
            # Una lectura estructurada posterior a una acción funciona como evidencia
            # de verificación, aunque la herramienta no se llame literalmente verify.
            session["verification_count"] += 1
        if is_action:
            session["action_seen"] = True

        if isinstance(result, dict):
            if result.get("contradiction") or result.get("conflict") or result.get("inconsistent"):
                self.record_contradiction()

    def _calculate(self, session: dict[str, Any], response_ok: bool = True) -> dict[str, Any]:
        kind = str(session.get("request_kind") or "factual")
        risk = str(session.get("risk_level") or "normal")
        base = {
            "simple_control": 0.68,
            "creative": 0.72,
            "current_state": 0.48,
            "factual": 0.48,
            "planning": 0.44,
            "diagnosis": 0.40,
        }.get(kind, 0.46)

        reads = int(session.get("structured_reads", 0))
        verified = int(session.get("verification_count", 0))
        successes = int(session.get("tool_success", 0))
        failures = int(session.get("tool_failure", 0))
        contradictions = int(session.get("contradiction_count", 0))
        deterministic = bool(session.get("deterministic"))
        trust = str(session.get("skill_trust") or "")

        score = base
        score += min(0.20, reads * 0.065)
        score += min(0.20, verified * 0.08)
        score += min(0.08, successes * 0.02)
        score -= min(0.36, failures * 0.15)
        score -= min(0.44, contradictions * 0.22)
        if deterministic:
            score += 0.16
        if trust == "verified":
            score += 0.08
        elif trust == "user":
            score += 0.04
        elif trust == "draft":
            score -= 0.04

        evidence_count = reads + verified
        min_high = max(1, int(self.config.get("minimum_evidence_for_high", 2)))
        if kind in {"diagnosis", "current_state", "factual", "planning"} and evidence_count == 0 and not deterministic:
            score = min(score, 0.54)
        if evidence_count < min_high and kind in {"diagnosis", "current_state", "factual", "planning"}:
            score = min(score, 0.76)
        if failures and successes == 0:
            score = min(score, 0.42)
        if risk == "high" and verified == 0:
            score = min(score, 0.66)
        if risk == "critical" and verified == 0:
            score = min(score, 0.54)
        elif risk == "critical":
            score = min(score, 0.82)
        if not response_ok:
            score = min(score, 0.18)

        score = max(0.05, min(0.98, score))
        low = float(self.config.get("low_threshold", 0.52))
        high = float(self.config.get("high_threshold", 0.78))
        band = "high" if score >= high else "medium" if score >= low else "low"
        escalation_threshold = float(self.config.get("escalation_candidate_threshold", 0.50))
        escalation_candidate = (
            score < escalation_threshold
            and kind in {"diagnosis", "current_state", "factual", "planning"}
        ) or contradictions > 0 or failures >= 2 or (risk in {"high", "critical"} and score < high)

        reasons = list(session.get("reason_codes") or [])
        if reads:
            reasons.append("structured_evidence")
        if verified:
            reasons.append("verified_evidence")
        if failures:
            reasons.append("tool_failures")
        if contradictions:
            reasons.append("contradictions")
        if deterministic:
            reasons.append("deterministic_route")
        if trust:
            reasons.append("skill_" + trust)
        if risk != "normal":
            reasons.append("risk_" + risk)
        if evidence_count == 0 and kind in {"diagnosis", "current_state", "factual", "planning"}:
            reasons.append("no_structured_evidence")

        return {
            "score": round(score, 3),
            "band": band,
            "request_kind": kind,
            "risk_level": risk,
            "evidence_count": evidence_count,
            "verification_count": verified,
            "failure_count": failures,
            "contradiction_count": contradictions,
            "deterministic": deterministic,
            "skill_trust": trust,
            "escalation_candidate": bool(escalation_candidate),
            "reason_codes": list(dict.fromkeys(reasons))[:20],
            "tool_names": list(session.get("tool_names") or [])[:40],
            "heuristic_not_probability": True,
        }

    def finish_request(self, *, response_ok: bool = True) -> dict[str, Any]:
        session = self._session()
        if session is None:
            session = {
                "request_kind": "factual", "risk_level": "normal", "tool_success": 0,
                "tool_failure": 0, "structured_reads": 0, "verification_count": 0,
                "contradiction_count": 0, "tool_names": [], "deterministic": False,
                "skill_trust": "", "reason_codes": ["missing_request_session"],
            }
        result = self._calculate(session, response_ok=response_ok)
        self._last = dict(result)
        if self.enabled and self.config.get("persist_assessments", True):
            self._persist(result)
        try:
            del self._local.session
        except Exception:
            pass
        return result

    def manual_assess(
        self,
        *, request_kind: str = "factual", risk_level: str = "normal",
        structured_reads: int = 0, verifications: int = 0, failures: int = 0,
        contradictions: int = 0, deterministic: bool = False, skill_trust: str = "",
    ) -> dict[str, Any]:
        session = {
            "request_kind": request_kind if request_kind in {"simple_control", "creative", "current_state", "factual", "planning", "diagnosis"} else "factual",
            "risk_level": risk_level if risk_level in {"normal", "high", "critical"} else "normal",
            "tool_success": max(0, structured_reads + verifications),
            "tool_failure": max(0, failures),
            "structured_reads": max(0, structured_reads),
            "verification_count": max(0, verifications),
            "contradiction_count": max(0, contradictions),
            "tool_names": [],
            "deterministic": bool(deterministic),
            "skill_trust": str(skill_trust or "").casefold(),
            "reason_codes": ["manual_structured_assessment"],
        }
        return self._calculate(session, response_ok=True)

    def _persist(self, result: dict[str, Any]):
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO confidence_assessments(
                    request_kind,risk_level,score,band,evidence_count,verification_count,
                    failure_count,contradiction_count,deterministic,skill_trust,
                    escalation_candidate,reason_codes_json,tool_names_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result["request_kind"], result["risk_level"], result["score"], result["band"],
                    result["evidence_count"], result["verification_count"], result["failure_count"],
                    result["contradiction_count"], 1 if result["deterministic"] else 0,
                    result.get("skill_trust", ""), 1 if result["escalation_candidate"] else 0,
                    json.dumps(result.get("reason_codes") or [], ensure_ascii=False),
                    json.dumps(result.get("tool_names") or [], ensure_ascii=False),
                ),
            )
            max_rows = max(100, int(self.config.get("max_assessments", 1800)))
            conn.execute(
                "DELETE FROM confidence_assessments WHERE id NOT IN (SELECT id FROM confidence_assessments ORDER BY id DESC LIMIT ?)",
                (max_rows,),
            )
            conn.commit()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        data = dict(row)
        for field in ("reason_codes_json", "tool_names_json"):
            key = field.removesuffix("_json")
            try:
                data[key] = json.loads(data.get(field) or "[]")
            except Exception:
                data[key] = []
            data.pop(field, None)
        data["deterministic"] = bool(data.get("deterministic"))
        data["escalation_candidate"] = bool(data.get("escalation_candidate"))
        data["heuristic_not_probability"] = True
        return data

    def last(self) -> dict[str, Any]:
        if self._last:
            return dict(self._last)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM confidence_assessments ORDER BY id DESC LIMIT 1").fetchone()
        return self._decode(row)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM confidence_assessments ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM confidence_assessments").fetchone()[0])
            low = int(conn.execute("SELECT COUNT(*) FROM confidence_assessments WHERE band='low'").fetchone()[0])
            candidates = int(conn.execute("SELECT COUNT(*) FROM confidence_assessments WHERE escalation_candidate=1").fetchone()[0])
        return {
            "enabled": self.enabled,
            "assessments": total,
            "low_assessments": low,
            "escalation_candidates": candidates,
            "low_threshold": float(self.config.get("low_threshold", 0.52)),
            "high_threshold": float(self.config.get("high_threshold", 0.78)),
            "score_is_calibrated_probability": False,
            "uses_llm_self_confidence": False,
            "persists_prompts": False,
            "persists_responses": False,
            "db_path": str(self.db_path),
            "last": self.last(),
        }


_instances: dict[tuple[str, int], ConfidenceEngine] = {}


def get_confidence_engine(config: dict[str, Any] | None = None, memory=None) -> ConfidenceEngine:
    cfg = config or {}
    root = Path(__file__).resolve().parent.parent
    key = (str(root), id(memory) if memory is not None else 0)
    engine = _instances.get(key)
    confidence_cfg = cfg.get("confidence", {}) if isinstance(cfg, dict) else {}
    if engine is None:
        engine = ConfidenceEngine(confidence_cfg, memory=memory)
        _instances[key] = engine
    else:
        engine.configure(confidence_cfg).attach_memory(memory)
    return engine
