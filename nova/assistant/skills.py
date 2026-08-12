from __future__ import annotations

"""Skills Engine declarativo de Nova.

Una Skill es un playbook local: parámetros + pasos + verificaciones + permisos
requeridos. No contiene código ejecutable y nunca concede permisos por sí sola.
La ejecución real vuelve a pasar por Agent/LocalTools y su política de seguridad.
"""

import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SKILLS_CONFIG: dict[str, Any] = {
    "enabled": True,
    "inject_context": True,
    "suggest_on_match": True,
    "suggest_threshold": 0.72,
    "explicit_run_threshold": 0.58,
    "max_prompt_skills": 3,
    "max_steps": 24,
    "max_skills": 500,
    "max_trigger_phrases": 20,
    "workspace_scoped_by_default": False,
    "generated_skills_start_as_draft": True,
    "auto_execute_matches": False,
}

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", re.I),
    re.compile(r"\b(?:password|passwd|token|api[_ -]?key|secret|cookie)\s*[:=]\s*[^\s]{8,}", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.I),
)
_SENSITIVE_PARAM = re.compile(r"(?:pass|password|passwd|token|secret|api.?key|cookie|credential|auth)", re.I)
_ALLOWED_PARAM_TYPES = {"string", "integer", "number", "boolean"}
_ALLOWED_TRUST = {"draft", "user", "verified"}


def _norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s_-]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _slug(value: Any) -> str:
    text = _norm(value).replace(" ", "-").replace("_", "-")
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:80] or "skill"


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _has_secret_material(value: Any) -> bool:
    try:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except Exception:
        text = str(value)
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _safe_run_args(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in (value or {}).items():
        name = str(key)[:80]
        if _SENSITIVE_PARAM.search(name):
            out[name] = "[REDACTED]"
        elif isinstance(item, (str, int, float, bool)) or item is None:
            out[name] = item if not isinstance(item, str) else item[:500]
        else:
            out[name] = "[COMPLEX_VALUE]"
    return out


def _tokens(value: str) -> set[str]:
    return {x for x in _norm(value).split() if len(x) >= 2}


@dataclass
class CompiledSkill:
    skill: dict[str, Any]
    arguments: dict[str, Any]
    steps: list[dict[str, Any]]
    verification: list[str]
    missing: list[str]


class SkillRegistry:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        memory=None,
        db_path: Path | None = None,
    ):
        self.config = dict(DEFAULT_SKILLS_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        self.memory = memory
        self.root = Path(__file__).resolve().parent.parent
        self.db_path = Path(db_path or (self.root / "data" / "skills.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def configure(self, config: dict[str, Any] | None = None):
        merged = dict(DEFAULT_SKILLS_CONFIG)
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
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL DEFAULT 'global',
                    workspace_id INTEGER,
                    slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    trust_level TEXT NOT NULL DEFAULT 'draft',
                    source TEXT NOT NULL DEFAULT 'nova',
                    trigger_phrases_json TEXT NOT NULL DEFAULT '[]',
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    verification_json TEXT NOT NULL DEFAULT '[]',
                    permissions_json TEXT NOT NULL DEFAULT '[]',
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    successful_runs INTEGER NOT NULL DEFAULT 0,
                    failed_runs INTEGER NOT NULL DEFAULT 0,
                    last_used_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(scope_key, slug)
                );
                CREATE INDEX IF NOT EXISTS idx_skills_scope ON skills(scope_key, enabled);
                CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);

                CREATE TABLE IF NOT EXISTS skill_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_skill_revisions ON skill_revisions(skill_id, version);

                CREATE TABLE IF NOT EXISTS skill_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id INTEGER NOT NULL,
                    workspace_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'prepared',
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    output_summary TEXT NOT NULL DEFAULT '',
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    finished_at DATETIME,
                    FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_skill_runs_skill ON skill_runs(skill_id, started_at);
                """
            )

    def _active_workspace_id(self) -> int | None:
        if self.memory is None or not hasattr(self.memory, "active_workspace"):
            return None
        try:
            row = self.memory.active_workspace()
            return int(row["id"]) if row else None
        except Exception:
            return None

    @staticmethod
    def _scope_key(workspace_id: int | None) -> str:
        return f"workspace:{int(workspace_id)}" if workspace_id is not None else "global"

    @staticmethod
    def _decode(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        for field, default in (
            ("trigger_phrases_json", []), ("parameters_json", {}), ("steps_json", []),
            ("verification_json", []), ("permissions_json", []), ("provenance_json", {}),
        ):
            key = field.removesuffix("_json")
            try:
                data[key] = json.loads(data.get(field) or json.dumps(default))
            except Exception:
                data[key] = default
            data.pop(field, None)
        data["enabled"] = bool(data.get("enabled"))
        return data

    def _validate_definition(
        self,
        name: str,
        description: str,
        triggers: list[Any],
        parameters: dict[str, Any],
        steps: list[Any],
        verification: list[Any],
        permissions: list[Any],
    ) -> tuple[list[str], dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
        clean_name = str(name or "").strip()
        if len(clean_name) < 2:
            raise ValueError("La habilidad necesita un nombre de al menos 2 caracteres.")
        if len(clean_name) > 120:
            raise ValueError("El nombre de la habilidad es demasiado largo.")

        trigger_limit = max(1, int(self.config.get("max_trigger_phrases", 20)))
        clean_triggers: list[str] = []
        for item in list(triggers or [])[:trigger_limit]:
            text = str(item or "").strip()
            if text and text not in clean_triggers:
                clean_triggers.append(text[:180])
        if not clean_triggers:
            clean_triggers = [clean_name]

        clean_params: dict[str, Any] = {}
        for raw_name, raw_spec in dict(parameters or {}).items():
            key = re.sub(r"[^A-Za-z0-9_]", "_", str(raw_name or "").strip())[:60]
            if not key:
                continue
            spec = dict(raw_spec) if isinstance(raw_spec, dict) else {"type": "string"}
            typ = str(spec.get("type") or "string").casefold()
            if typ not in _ALLOWED_PARAM_TYPES:
                raise ValueError(f"Tipo de parámetro no soportado para {key}: {typ}")
            clean_params[key] = {
                "type": typ,
                "required": bool(spec.get("required", False)),
                "description": str(spec.get("description") or "")[:240],
            }
            if "default" in spec and not _SENSITIVE_PARAM.search(key):
                default = spec.get("default")
                if isinstance(default, (str, int, float, bool)) or default is None:
                    clean_params[key]["default"] = default if not isinstance(default, str) else default[:500]

        max_steps = max(1, min(int(self.config.get("max_steps", 24)), 60))
        clean_steps: list[dict[str, Any]] = []
        for idx, raw in enumerate(list(steps or [])[:max_steps], start=1):
            if isinstance(raw, str):
                raw = {"instruction": raw}
            if not isinstance(raw, dict):
                continue
            instruction = str(raw.get("instruction") or raw.get("action") or "").strip()
            if not instruction:
                continue
            clean_steps.append({
                "title": str(raw.get("title") or f"Paso {idx}")[:120],
                "instruction": instruction[:1800],
                "tool_hint": str(raw.get("tool_hint") or raw.get("tool") or "")[:120],
                "verify": str(raw.get("verify") or "")[:700],
                "optional": bool(raw.get("optional", False)),
            })
        if not clean_steps:
            raise ValueError("La habilidad necesita al menos un paso declarativo.")

        clean_verification = [str(x).strip()[:700] for x in list(verification or []) if str(x).strip()][:16]
        clean_permissions = []
        for item in list(permissions or []):
            text = _norm(item).replace(" ", "_")[:80]
            if text and text not in clean_permissions:
                clean_permissions.append(text)

        payload = {
            "name": clean_name,
            "description": str(description or "")[:1200],
            "triggers": clean_triggers,
            "parameters": clean_params,
            "steps": clean_steps,
            "verification": clean_verification,
            "permissions": clean_permissions,
        }
        if _has_secret_material(payload):
            raise ValueError("La definición parece contener credenciales o secretos. Elimínalos y usa parámetros en su lugar.")
        return clean_triggers, clean_params, clean_steps, clean_verification, clean_permissions

    def save(
        self,
        name: str,
        description: str = "",
        triggers: list[Any] | None = None,
        parameters: dict[str, Any] | None = None,
        steps: list[Any] | None = None,
        verification: list[Any] | None = None,
        permissions: list[Any] | None = None,
        workspace_id: int | None = None,
        source: str = "nova",
        trust_level: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Skills Engine está desactivado.")
        triggers2, params2, steps2, verify2, perms2 = self._validate_definition(
            name, description, triggers or [], parameters or {}, steps or [], verification or [], permissions or []
        )
        if workspace_id is None and bool(self.config.get("workspace_scoped_by_default", False)):
            workspace_id = self._active_workspace_id()
        scope_key = self._scope_key(workspace_id)
        slug = _slug(name)
        source = _norm(source or "nova").replace(" ", "_")[:80] or "nova"
        if trust_level is None:
            trust_level = "draft" if self.config.get("generated_skills_start_as_draft", True) and source not in {"user", "manual"} else "user"
        trust_level = str(trust_level).casefold()
        if trust_level not in _ALLOWED_TRUST:
            trust_level = "draft"
        prov = dict(provenance or {})
        if _has_secret_material(prov):
            prov = {"note": "provenance_redacted"}

        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT * FROM skills WHERE scope_key=? AND slug=?", (scope_key, slug)).fetchone()
            if existing:
                old = self._decode(existing)
                snapshot = json.dumps(old, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    "INSERT INTO skill_revisions(skill_id,version,snapshot_json) VALUES (?,?,?)",
                    (int(existing["id"]), int(existing["version"]), snapshot),
                )
                version = int(existing["version"]) + 1
                conn.execute(
                    """UPDATE skills SET name=?,description=?,enabled=1,version=?,trust_level=?,source=?,
                       trigger_phrases_json=?,parameters_json=?,steps_json=?,verification_json=?,permissions_json=?,
                       provenance_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        str(name).strip()[:120], str(description or "")[:1200], version, trust_level, source,
                        json.dumps(triggers2, ensure_ascii=False), json.dumps(params2, ensure_ascii=False),
                        json.dumps(steps2, ensure_ascii=False), json.dumps(verify2, ensure_ascii=False),
                        json.dumps(perms2, ensure_ascii=False), json.dumps(prov, ensure_ascii=False), int(existing["id"]),
                    ),
                )
                skill_id = int(existing["id"])
            else:
                count = int(conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0])
                if count >= int(self.config.get("max_skills", 500)):
                    raise RuntimeError("Se alcanzó el límite configurado de habilidades.")
                cur = conn.execute(
                    """INSERT INTO skills(scope_key,workspace_id,slug,name,description,trust_level,source,
                       trigger_phrases_json,parameters_json,steps_json,verification_json,permissions_json,provenance_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        scope_key, int(workspace_id) if workspace_id is not None else None, slug,
                        str(name).strip()[:120], str(description or "")[:1200], trust_level, source,
                        json.dumps(triggers2, ensure_ascii=False), json.dumps(params2, ensure_ascii=False),
                        json.dumps(steps2, ensure_ascii=False), json.dumps(verify2, ensure_ascii=False),
                        json.dumps(perms2, ensure_ascii=False), json.dumps(prov, ensure_ascii=False),
                    ),
                )
                skill_id = int(cur.lastrowid)
            conn.commit()
        return self.get(skill_id)

    def get(self, skill: int | str, workspace_id: int | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = None
            if isinstance(skill, int) or str(skill).isdigit():
                row = conn.execute("SELECT * FROM skills WHERE id=?", (int(skill),)).fetchone()
            else:
                slug = _slug(skill)
                wid = self._active_workspace_id() if workspace_id is None else workspace_id
                if wid is not None:
                    row = conn.execute(
                        "SELECT * FROM skills WHERE slug=? AND scope_key IN (?, 'global') ORDER BY CASE WHEN workspace_id=? THEN 0 ELSE 1 END LIMIT 1",
                        (slug, self._scope_key(wid), int(wid)),
                    ).fetchone()
                else:
                    row = conn.execute("SELECT * FROM skills WHERE slug=? AND scope_key='global' LIMIT 1", (slug,)).fetchone()
            return self._decode(row) if row else None

    def list(self, workspace_id: int | None = None, include_disabled: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        wid = self._active_workspace_id() if workspace_id is None else workspace_id
        clauses = ["scope_key='global'"]
        args: list[Any] = []
        if wid is not None:
            clauses.append("workspace_id=?")
            args.append(int(wid))
        enabled = "" if include_disabled else "AND enabled=1"
        query = f"SELECT * FROM skills WHERE ({' OR '.join(clauses)}) {enabled} ORDER BY updated_at DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            return [self._decode(row) for row in conn.execute(query, args).fetchall()]

    def set_enabled(self, skill: int | str, enabled: bool, workspace_id: int | None = None) -> dict[str, Any] | None:
        row = self.get(skill, workspace_id=workspace_id)
        if not row:
            return None
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE skills SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (1 if enabled else 0, int(row["id"])))
            conn.commit()
        return self.get(int(row["id"]))

    def match(self, text: str, workspace_id: int | None = None, limit: int = 5) -> list[dict[str, Any]]:
        query = _norm(text)
        if not query:
            return []
        qtokens = _tokens(query)
        rows = self.list(workspace_id=workspace_id, include_disabled=False, limit=500)
        scored: list[dict[str, Any]] = []
        for row in rows:
            name = _norm(row.get("name"))
            slug = _norm(str(row.get("slug") or "").replace("-", " "))
            triggers = [_norm(x) for x in row.get("trigger_phrases") or []]
            score = 0.0
            reason = ""
            if query in {name, slug}:
                score, reason = 1.0, "nombre exacto"
            else:
                for trigger in triggers:
                    if not trigger:
                        continue
                    if query == trigger:
                        score, reason = 0.99, "trigger exacto"
                        break
                    if trigger in query and len(trigger) >= 5:
                        candidate = min(0.96, 0.78 + min(len(trigger), 60) / 350)
                        if candidate > score:
                            score, reason = candidate, "trigger contenido"
                    overlap = len(qtokens & _tokens(trigger))
                    union = len(qtokens | _tokens(trigger)) or 1
                    candidate = overlap / union
                    if overlap >= 2 and candidate > score:
                        score, reason = min(0.82, candidate + 0.18), "similitud léxica"
                desc_tokens = _tokens(str(row.get("description") or ""))
                if qtokens and desc_tokens:
                    overlap = len(qtokens & desc_tokens)
                    if overlap >= 2:
                        score = max(score, min(0.68, 0.25 + overlap / max(4, len(qtokens))))
                        reason = reason or "descripción relacionada"
            if row.get("workspace_id") is not None:
                score = min(1.0, score + 0.04)
            if score > 0:
                item = dict(row)
                item["match_score"] = round(score, 3)
                item["match_reason"] = reason
                scored.append(item)
        scored.sort(key=lambda x: (float(x.get("match_score", 0)), int(x.get("successful_runs", 0))), reverse=True)
        return scored[: max(1, min(int(limit), 20))]

    @staticmethod
    def _coerce(value: Any, typ: str) -> Any:
        if value is None:
            return None
        if typ == "string":
            return str(value)
        if typ == "integer":
            return int(value)
        if typ == "number":
            return float(value)
        if typ == "boolean":
            if isinstance(value, bool):
                return value
            return _norm(value) in {"1", "true", "si", "sí", "yes", "on"}
        return value

    def compile(self, skill: int | str | dict[str, Any], arguments: dict[str, Any] | None = None) -> CompiledSkill:
        row = skill if isinstance(skill, dict) else self.get(skill)
        if not row:
            raise KeyError(f"No encontré la habilidad: {skill}")
        if not row.get("enabled"):
            raise RuntimeError("La habilidad está deshabilitada.")
        specs = dict(row.get("parameters") or {})
        supplied = dict(arguments or {})
        bound: dict[str, Any] = {}
        missing: list[str] = []
        for name, spec in specs.items():
            if name in supplied:
                try:
                    bound[name] = self._coerce(supplied[name], str(spec.get("type") or "string"))
                except Exception:
                    raise ValueError(f"Valor inválido para el parámetro {name}.")
            elif "default" in spec:
                bound[name] = spec.get("default")
            elif spec.get("required"):
                missing.append(name)
        for name, value in supplied.items():
            if name not in bound and name not in specs:
                bound[str(name)] = value

        class SafeMap(dict):
            def __missing__(self, key):
                return "{" + str(key) + "}"

        mapping = SafeMap({k: str(v) for k, v in bound.items()})
        steps: list[dict[str, Any]] = []
        for step in row.get("steps") or []:
            item = dict(step)
            for field in ("title", "instruction", "tool_hint", "verify"):
                item[field] = str(item.get(field) or "").format_map(mapping)
            steps.append(item)
        verification = [str(x).format_map(mapping) for x in (row.get("verification") or [])]
        return CompiledSkill(row, bound, steps, verification, missing)

    def format_playbook(self, compiled: CompiledSkill, run_id: int | None = None) -> str:
        skill = compiled.skill
        lines = [
            f"SKILL: {skill.get('name')} · v{skill.get('version')} · confianza={skill.get('trust_level')}",
            f"Descripción: {skill.get('description') or '(sin descripción)'}",
            "IMPORTANTE: esta Skill es un playbook local, NO un permiso. Todas las acciones siguen sujetas a seguridad/confirmaciones normales.",
        ]
        if run_id:
            lines.append(f"Run ID: {run_id}")
        if compiled.arguments:
            visible = _safe_run_args(compiled.arguments)
            lines.append("Parámetros: " + json.dumps(visible, ensure_ascii=False))
        if compiled.missing:
            lines.append("FALTAN PARÁMETROS REQUERIDOS: " + ", ".join(compiled.missing))
        lines.append("PASOS:")
        for idx, step in enumerate(compiled.steps, start=1):
            suffix = " (opcional)" if step.get("optional") else ""
            lines.append(f"{idx}. {step.get('title')}{suffix}: {step.get('instruction')}")
            if step.get("tool_hint"):
                lines.append(f"   Herramienta sugerida: {step.get('tool_hint')}")
            if step.get("verify"):
                lines.append(f"   Verifica: {step.get('verify')}")
        checks = list(compiled.verification or [])
        if checks:
            lines.append("VERIFICACIÓN FINAL:")
            for check in checks:
                lines.append(f"- {check}")
        permissions = list(skill.get("permissions") or [])
        if permissions:
            lines.append("CAPACIDADES REQUERIDAS (declarativas, no concedidas): " + ", ".join(permissions))
        return "\n".join(lines)

    def start_run(self, compiled: CompiledSkill, workspace_id: int | None = None) -> int:
        if compiled.missing:
            raise ValueError("Faltan parámetros: " + ", ".join(compiled.missing))
        if workspace_id is None:
            workspace_id = self._active_workspace_id()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO skill_runs(skill_id,workspace_id,status,arguments_json,steps_json) VALUES (?,?,?,?,?)",
                (
                    int(compiled.skill["id"]), int(workspace_id) if workspace_id is not None else None, "prepared",
                    json.dumps(_safe_run_args(compiled.arguments), ensure_ascii=False),
                    json.dumps(compiled.steps, ensure_ascii=False),
                ),
            )
            conn.execute("UPDATE skills SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (int(compiled.skill["id"]),))
            conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, success: bool | None, summary: str = "") -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM skill_runs WHERE id=?", (int(run_id),)).fetchone()
            if not row:
                return None
            if success is True:
                status = "completed"
            elif success is False:
                status = "failed"
            else:
                status = "agent_returned"
            conn.execute(
                "UPDATE skill_runs SET status=?,output_summary=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, str(summary or "")[:1000], int(run_id)),
            )
            if success is True:
                conn.execute("UPDATE skills SET successful_runs=successful_runs+1 WHERE id=?", (int(row["skill_id"]),))
            elif success is False:
                conn.execute("UPDATE skills SET failed_runs=failed_runs+1 WHERE id=?", (int(row["skill_id"]),))
            counts = conn.execute("SELECT successful_runs,failed_runs,trust_level FROM skills WHERE id=?", (int(row["skill_id"]),)).fetchone()
            if counts and counts["trust_level"] == "draft" and int(counts["successful_runs"]) >= 2:
                conn.execute("UPDATE skills SET trust_level='verified',updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(row["skill_id"]),))
            conn.commit()
        return self.run_info(int(run_id))

    def run_info(self, run_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM skill_runs WHERE id=?", (int(run_id),)).fetchone()
            if not row:
                return None
            data = dict(row)
            for field, default in (("arguments_json", {}), ("steps_json", [])):
                key = field.removesuffix("_json")
                try:
                    data[key] = json.loads(data.get(field) or json.dumps(default))
                except Exception:
                    data[key] = default
                data.pop(field, None)
            return data

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.*,s.name AS skill_name FROM skill_runs r
                   JOIN skills s ON s.id=r.skill_id ORDER BY r.id DESC LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def revisions(self, skill: int | str, limit: int = 20) -> list[dict[str, Any]]:
        row = self.get(skill)
        if not row:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT version,snapshot_json,created_at FROM skill_revisions WHERE skill_id=? ORDER BY version DESC LIMIT ?",
                (int(row["id"]), max(1, min(int(limit), 100))),
            ).fetchall()
            out = []
            for item in rows:
                try:
                    snapshot = json.loads(item["snapshot_json"])
                except Exception:
                    snapshot = {}
                out.append({"version": item["version"], "created_at": item["created_at"], "snapshot": snapshot})
            return out

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0])
            enabled = int(conn.execute("SELECT COUNT(*) FROM skills WHERE enabled=1").fetchone()[0])
            drafts = int(conn.execute("SELECT COUNT(*) FROM skills WHERE trust_level='draft'").fetchone()[0])
            verified = int(conn.execute("SELECT COUNT(*) FROM skills WHERE trust_level='verified'").fetchone()[0])
            runs = int(conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0])
        return {
            "enabled": self.enabled,
            "skills": total,
            "enabled_skills": enabled,
            "drafts": drafts,
            "verified": verified,
            "runs": runs,
            "db_path": str(self.db_path),
            "db_size_mb": round(self.db_path.stat().st_size / 1024**2, 3) if self.db_path.exists() else 0.0,
            "auto_execute_matches": bool(self.config.get("auto_execute_matches", False)),
            "declarative_only": True,
            "inherits_security_policy": True,
        }

    def compact_candidates(self, text: str, workspace_id: int | None = None) -> str:
        if not self.enabled or not self.config.get("inject_context", True) or not self.config.get("suggest_on_match", True):
            return ""
        threshold = float(self.config.get("suggest_threshold", 0.72))
        limit = max(1, min(int(self.config.get("max_prompt_skills", 3)), 6))
        rows = [x for x in self.match(text, workspace_id=workspace_id, limit=limit) if float(x.get("match_score", 0)) >= threshold]
        if not rows:
            return ""
        lines = ["Skills posiblemente relevantes (solo sugerencias; no ejecutar automáticamente):"]
        for row in rows:
            scope = "workspace" if row.get("workspace_id") is not None else "global"
            lines.append(
                f"- {row.get('name')} · {float(row.get('match_score',0))*100:.0f}% · {scope} · "
                f"{row.get('description') or 'sin descripción'}"
            )
        return "\n".join(lines)


_instances: dict[tuple[str, int], SkillRegistry] = {}


def get_skill_registry(config: dict[str, Any] | None = None, memory=None) -> SkillRegistry:
    cfg = config or {}
    root = Path(__file__).resolve().parent.parent
    key = (str(root), id(memory) if memory is not None else 0)
    registry = _instances.get(key)
    skill_cfg = cfg.get("skills", {}) if isinstance(cfg, dict) else {}
    if registry is None:
        registry = SkillRegistry(skill_cfg, memory=memory)
        _instances[key] = registry
    else:
        registry.configure(skill_cfg).attach_memory(memory)
    return registry
