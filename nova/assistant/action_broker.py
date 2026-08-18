from __future__ import annotations

"""Single local authority for permissioned tool execution."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any, Callable

from .action_apps import classify_application, classify_document
from .action_context import ActionContext
from .action_powershell import classify_powershell


ACTION_STATES = {"pending", "approved", "denied", "expired", "cancelled", "executed"}
EFFECT_CLASSES = {"read_only", "sensitive_read", "reversible", "mutating", "high_risk", "forbidden"}


@dataclass(frozen=True)
class ActionPolicy:
    effect: str
    risk: str
    reason: str
    allow_task_grant: bool = False


_READ_ONLY = {
    "system_status", "list_processes", "list_files", "read_file", "web_search",
    "browser_read", "browser_inspect", "browser_tabs", "window_list", "uia_snapshot",
    "memory_search", "workspace_list", "workspace_info", "workspace_changes", "workspace_search",
    "workspace_index_status", "memory_semantic_status", "continuity_resume", "continuity_pending",
    "continuity_history", "performance_summary", "performance_recent", "llm_performance_summary",
    "doctor_repairs", "perception_context", "perception_status", "perception_recent",
    "context_activity", "context_relevant_recent", "context_intelligence_status",
    "workspace_autodetect_status", "workspace_autodetect_associations", "anomaly_status", "anomaly_recent",
    "vision_status", "vision_last", "vision_recent_events", "skill_status", "skill_list", "skill_search",
    "skill_info", "skill_runs", "skill_reliability_status", "skill_reliability_report",
    "skill_reliability_review_queue", "expert_status", "expert_recent", "expert_learning_status",
    "expert_learning_candidate", "expert_learning_recent", "confidence_status", "confidence_last",
    "confidence_recent", "nova_version_status", "nova_whats_new", "nova_repository_activity",
    "nova_repository_file",
}
_SENSITIVE_READ = {"clipboard_read", "screenshot", "vision_describe_screen", "expert_import_chatgpt_response"}
_REVERSIBLE = {
    "open_app", "open_document", "open_url", "browser_open", "browser_search", "browser_back", "browser_reload",
    "window_activate", "window_move", "mouse_move", "workspace_open", "workspace_set_active",
    "workspace_index", "remember", "continuity_checkpoint", "anomaly_acknowledge", "confidence_assess",
}
_MUTATING = {
    "write_file", "clipboard_write", "browser_click", "browser_fill", "window_close", "uia_type",
    "keyboard_type", "workspace_create", "memory_semantic_reindex", "continuity_close",
    "workspace_autodetect_learn_current", "workspace_autodetect_forget_current",
    "anomaly_mark_process_expected", "skill_set_enabled", "skill_create", "skill_run",
    "expert_learning_verify", "expert_learning_save_skill", "expert_learning_discard",
    "expert_prepare_chatgpt", "expert_free_second_opinion",
}
_HIGH_RISK = {"powershell", "browser_press", "keyboard_press", "uia_click", "mouse_click"}
_SENSITIVE_TARGET = re.compile(
    r"(?i)\b(buy|purchase|checkout|pay|transfer|send|post|publish|delete|remove|"
    r"submit|comprar|pagar|transferir|enviar|publicar|eliminar|borrar|confirmar|pedido)\b"
)


def _registry() -> dict[str, ActionPolicy]:
    registry: dict[str, ActionPolicy] = {}
    groups = (
        (_READ_ONLY, ActionPolicy("read_only", "low", "Lectura local o pública sin efectos.")),
        (_SENSITIVE_READ, ActionPolicy("sensitive_read", "medium", "Lectura sensible solicitada explícitamente.")),
        (_REVERSIBLE, ActionPolicy("reversible", "low", "Efecto local reversible.", allow_task_grant=True)),
        (_MUTATING | {"skill_save", "skill_finish"}, ActionPolicy("mutating", "medium", "La acción modifica estado.", allow_task_grant=True)),
        (_HIGH_RISK, ActionPolicy("high_risk", "high", "Acción de alto riesgo; permiso de una sola vez.")),
    )
    for names, policy in groups:
        for name in names:
            if name in registry:
                raise RuntimeError(f"duplicate_action_policy:{name}")
            registry[name] = policy
    return registry


POLICY_REGISTRY = _registry()


def validate_policy_coverage(tool_names: set[str] | list[str]) -> None:
    names = {str(name) for name in tool_names if str(name)}
    missing = sorted(names - set(POLICY_REGISTRY))
    if missing:
        raise ValueError("unclassified_action_tools:" + ",".join(missing))


def _browser_click_policy(context: ActionContext | None) -> ActionPolicy:
    observations = context.observations if isinstance(context, ActionContext) else {}
    inspection = str(observations.get("browser_inspection") or "unavailable")
    control = observations.get("browser_control") if isinstance(observations.get("browser_control"), dict) else {}
    if inspection != "ok" or not control:
        return ActionPolicy("high_risk", "high", "El control web no pudo inspeccionarse de forma concluyente; permiso de una sola vez.")
    tag = str(control.get("tag") or "").casefold()
    if tag == "a" and control.get("passive_link") is True:
        return POLICY_REGISTRY["browser_click"]
    return ActionPolicy(
        "high_risk", "high",
        "El control no es un enlace HTTP(S) pasivo demostrado; requiere permiso de una sola vez.",
    )


def policy_for(
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    known_tools: set[str] | None = None,
    context: ActionContext | None = None,
) -> ActionPolicy:
    name = str(tool or "")
    args = dict(arguments or {})
    if known_tools is not None and name not in known_tools:
        return ActionPolicy("forbidden", "critical", "Herramienta desconocida: política fail-closed.")
    if name not in POLICY_REGISTRY:
        return ActionPolicy("forbidden", "critical", "Herramienta sin política explícita: acción prohibida.")
    if name == "powershell":
        assessment = classify_powershell(str(args.get("command") or ""))
        if not assessment.allowed:
            return ActionPolicy("forbidden", "critical", assessment.reason)
        return ActionPolicy("high_risk", "high", assessment.reason)
    if name == "open_app":
        target = classify_application(str(args.get("app") or ""))
        if not target.allowed:
            return ActionPolicy("forbidden", "critical", target.reason)
        if isinstance(context, ActionContext):
            observed = context.observations.get("application")
            file_state = observed.get("file") if isinstance(observed, dict) else None
            if (
                not isinstance(observed, dict) or observed.get("allowed") is not True
                or not isinstance(file_state, dict) or file_state.get("exists") is not True
                or file_state.get("kind") != "file"
            ):
                return ActionPolicy("forbidden", "critical", "Aplicación registrada no verificable en una ubicación confiable.")
    if name == "open_document":
        target = classify_document(str(args.get("path") or ""))
        if not target.allowed:
            return ActionPolicy("forbidden", "critical", target.reason)
        if isinstance(context, ActionContext):
            file_state = context.observations.get("file")
            if (
                "file_error" in context.observations
                or not isinstance(file_state, dict) or file_state.get("exists") is not True
                or file_state.get("kind") != "file"
            ):
                return ActionPolicy("forbidden", "critical", "Documento fuera de scope, ausente o no verificable.")
    if name == "browser_fill" and bool(args.get("submit")):
        return ActionPolicy("high_risk", "high", "Rellenar y enviar un formulario requiere permiso de una sola vez.")
    if name == "browser_click":
        return _browser_click_policy(context)
    if name == "uia_click" and _SENSITIVE_TARGET.search(str(args.get("target") or args.get("control") or "")):
        return ActionPolicy("high_risk", "high", "El control puede producir un efecto externo sensible.")
    return POLICY_REGISTRY[name]


def policy_registry(tool_names: set[str] | list[str]) -> dict[str, ActionPolicy]:
    names = {str(name) for name in tool_names if str(name)}
    validate_policy_coverage(names)
    return {name: POLICY_REGISTRY[name] for name in sorted(names)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ActionBroker:
    """Thread-safe broker. Only its installed local UI handler may approve."""

    def __init__(self, config=None, *, tool_names=None, audit_path: Path | None = None, clock=time.monotonic):
        self.config = config or {}
        self.known_tools = {str(x) for x in (tool_names or []) if str(x)}
        self.audit_path = Path(audit_path) if audit_path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._task_grants: set[tuple[str, str, str, str, str]] = set()
        self._approval_handler: Callable[[dict[str, Any]], Any] | None = None
        self._state_listener: Callable[[str, dict[str, Any]], Any] | None = None
        self._shutting_down = False
        self._terminal_sequence = 0
        self.total_wait_seconds = 0.0
        security = self.config.get("security", {}) if isinstance(self.config, dict) else {}
        self._history_limit = max(16, min(int(security.get("action_history_limit", 256) or 256), 2048))
        self._active_limit = max(1, min(int(security.get("action_active_limit", 32) or 32), 256))
        self._audit_max_bytes = max(4096, min(int(security.get("action_audit_max_bytes", 262144) or 262144), 8 * 1024 * 1024))
        self._audit_rotations = max(0, min(int(security.get("action_audit_rotations", 2) or 2), 5))

    @property
    def profile(self) -> str:
        security = self.config.get("security", {}) if isinstance(self.config, dict) else {}
        value = str(security.get("profile") or "balanced").casefold()
        return value if value in {"safe", "balanced", "trusted"} else "balanced"

    def set_approval_handler(self, handler: Callable[[dict[str, Any]], Any] | None) -> None:
        with self._lock:
            self._approval_handler = handler

    def set_state_listener(self, listener: Callable[[str, dict[str, Any]], Any] | None) -> None:
        with self._lock:
            self._state_listener = listener

    def _requires_approval(self, policy: ActionPolicy) -> bool:
        if policy.effect in {"forbidden", "high_risk"}:
            return True
        if policy.effect == "sensitive_read":
            return self.profile == "safe"
        if self.profile == "safe":
            return policy.effect in {"reversible", "mutating"}
        if self.profile == "balanced":
            return policy.effect == "mutating"
        return False

    def _atomic_audit_write_locked(self, line: bytes) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = self.audit_path.read_bytes()
        except FileNotFoundError:
            current = b""
        if current and len(current) + len(line) > self._audit_max_bytes:
            for index in range(self._audit_rotations, 0, -1):
                source = self.audit_path if index == 1 else self.audit_path.with_name(self.audit_path.name + f".{index - 1}")
                destination = self.audit_path.with_name(self.audit_path.name + f".{index}")
                try:
                    if source.exists():
                        os.replace(source, destination)
                except OSError:
                    pass
            current = b""
        tmp = self.audit_path.with_name(self.audit_path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(current)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.audit_path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _audit(self, event: str, request: dict[str, Any], *, detail: str = "") -> None:
        if self.audit_path is None:
            return
        payload = {
            "timestamp": _now_iso(), "event": str(event), "request_id": request.get("request_id"),
            "tool": request.get("tool"), "effect": request.get("effect"), "risk": request.get("risk"),
            "target_sha256": hashlib.sha256(str(request.get("target") or "").encode("utf-8", errors="ignore")).hexdigest(),
            "task_id": request.get("task_id"),
            "owner_id": request.get("owner_id"), "scope": request.get("scope"),
            "session_id": request.get("session_id"),
            "arguments_sha256": request.get("arguments_sha256"), "state": request.get("state"),
            "detail": str(detail or "")[:160],
        }
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > self._audit_max_bytes:
            return
        try:
            with self._lock:
                self._atomic_audit_write_locked(line)
        except OSError:
            pass

    def _emit_state_locked(self, event: str, request: dict[str, Any]) -> None:
        if self._state_listener is not None:
            try:
                self._state_listener(str(event), self._public(request))
            except Exception:
                pass

    def _prune_locked(self) -> int:
        terminal = sorted(
            (row for row in self._pending.values() if row.get("state") in {"denied", "expired", "cancelled", "executed"}),
            key=lambda row: int(row.get("terminal_sequence") or 0),
        )
        remove = max(0, len(terminal) - self._history_limit)
        for row in terminal[:remove]:
            self._pending.pop(str(row.get("request_id") or ""), None)
        return remove

    def _expire_locked(self, detail: str = "sweep") -> int:
        now = self._clock()
        expired = 0
        for row in list(self._pending.values()):
            if (
                row.get("state") in {"pending", "approved"}
                and int(row.get("executions") or 0) == 0
                and now >= float(row.get("expires_at") or 0)
            ):
                self._mark_locked(row, "expired", detail=detail)
                expired += 1
        return expired

    def prune(self) -> int:
        with self._lock:
            self._expire_locked("prune_sweep")
            return self._prune_locked()

    def _mark_locked(self, request: dict[str, Any], state: str, *, detail: str = "") -> None:
        request["state"] = state
        if state in {"denied", "expired", "cancelled", "executed"}:
            self._terminal_sequence += 1
            request["terminal_sequence"] = self._terminal_sequence
        event = request.get("event")
        if state != "pending" and hasattr(event, "set"):
            event.set()
        self._audit(state, request, detail=detail)
        self._emit_state_locked(state, request)
        self._prune_locked()

    def _public(self, request: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "request_id", "tool", "effect", "risk", "target", "reason", "task_id", "owner_id",
            "scope", "session_id", "created_at", "expires_at", "state", "allow_task_grant",
        )
        return {key: request.get(key) for key in keys}

    def request(self, tool: str, arguments: dict[str, Any], context: ActionContext, *, timeout=None) -> dict[str, Any]:
        policy = policy_for(tool, arguments, known_tools=self.known_tools, context=context)
        if policy.effect == "forbidden":
            denied = {
                "request_id": uuid.uuid4().hex, "tool": tool, "effect": policy.effect, "risk": policy.risk,
                "target": context.target, "reason": policy.reason, "task_id": context.task_id or None,
                "owner_id": context.owner_id, "scope": context.scope, "session_id": context.session_id,
                "arguments_sha256": context.arguments_sha256, "context_sha256": context.digest,
                "state": "denied", "created_at": _now_iso(), "expires_at": None,
            }
            self._audit("denied", denied, detail="forbidden")
            return {"ok": False, "error": "forbidden_action", "authorization_state": "denied", "request": self._public(denied)}
        has_local_human_intent = bool(
            context.explicit_intent
            and context.intent_source == "local_user"
            and len(context.intent_sha256) == 64
            and context.intent_session_id == context.session_id
            and bool(re.fullmatch(r"[0-9a-f]{32}", context.intent_request_id))
        )
        if policy.effect == "sensitive_read" and not has_local_human_intent:
            return {"ok": False, "error": "explicit_intent_required", "authorization_state": "denied"}
        with self._lock:
            self._expire_locked("request_entry_sweep")
        if policy.effect == "read_only" or not self._requires_approval(policy):
            return {"ok": True, "authorization_state": "approved", "automatic": True, "policy": policy}

        wait_seconds = float(timeout if timeout is not None else self.config.get("security", {}).get("approval_timeout_seconds", 120) or 120)
        wait_seconds = max(1.0, min(wait_seconds, 900.0))
        event = threading.Event()
        created = self._clock()
        request = {
            "request_id": uuid.uuid4().hex, "tool": str(tool), "effect": policy.effect, "risk": policy.risk,
            "target": context.target, "reason": policy.reason, "task_id": context.task_id or None,
            "owner_id": context.owner_id, "scope": context.scope, "session_id": context.session_id,
            "arguments_sha256": context.arguments_sha256, "context_sha256": context.digest,
            "created_at": _now_iso(), "expires_at": created + wait_seconds, "state": "pending",
            "allow_task_grant": bool(policy.allow_task_grant and policy.effect != "high_risk"),
            "executions": 0, "event": event, "policy": policy,
        }
        with self._lock:
            self._expire_locked("request_create_sweep")
            if self._shutting_down:
                return {"ok": False, "error": "authorization_cancelled", "authorization_state": "cancelled"}
            for existing in self._pending.values():
                if (
                    existing.get("state") == "approved"
                    and existing.get("tool") == str(tool)
                    and existing.get("context_sha256") == context.digest
                    and int(existing.get("executions") or 0) == 0
                ):
                    return {
                        "ok": True,
                        "authorization_state": "approved",
                        "request_id": existing.get("request_id"),
                        "policy": policy,
                        "resumed": True,
                    }
            grant_key = (context.task_id, context.owner_id, context.scope, context.session_id, str(tool))
            if policy.allow_task_grant and context.task_id and grant_key in self._task_grants:
                return {"ok": True, "authorization_state": "approved", "automatic": True, "task_grant": True, "policy": policy}
            handler = self._approval_handler
            if handler is None:
                unavailable = {
                    "request_id": uuid.uuid4().hex, "tool": str(tool), "effect": policy.effect, "risk": policy.risk,
                    "target": context.target, "reason": policy.reason, "task_id": context.task_id or None,
                    "owner_id": context.owner_id, "scope": context.scope, "session_id": context.session_id,
                    "arguments_sha256": context.arguments_sha256, "context_sha256": context.digest,
                    "state": "cancelled", "created_at": _now_iso(), "expires_at": None,
                }
                self._audit("cancelled", unavailable, detail="approval_ui_unavailable")
                return {"ok": False, "error": "approval_ui_unavailable", "authorization_state": "cancelled", "request": self._public(unavailable)}
            active = sum(1 for row in self._pending.values() if row.get("state") in {"pending", "approved"})
            if active >= self._active_limit:
                return {"ok": False, "error": "authorization_capacity_exceeded", "authorization_state": "denied"}
            self._pending[request["request_id"]] = request
            self._audit("pending", request)
            self._emit_state_locked("pending", request)
        try:
            handler(self._public(request))
        except Exception:
            self.deny(request["request_id"], reason="approval_ui_error")

        wait_started = self._clock()
        event.wait(wait_seconds)
        waited = max(0.0, self._clock() - wait_started)
        with self._lock:
            self.total_wait_seconds += waited
            self._expire_locked("wait_complete_sweep")
            if request["state"] == "pending":
                self._mark_locked(request, "expired", detail="wait_timeout")
        if request["state"] != "approved":
            return {
                "ok": False,
                "error": "authorization_" + str(request["state"]),
                "authorization_state": request["state"],
                "request": self._public(request),
            }
        return {"ok": True, "authorization_state": "approved", "request_id": request["request_id"], "policy": policy}

    def approve(self, request_id: str, *, mode: str = "once") -> bool:
        with self._lock:
            request = self._pending.get(str(request_id))
            self._expire_locked("approval_sweep")
            if not request or request.get("state") != "pending" or self._shutting_down:
                return False
            if mode == "task":
                if not request.get("allow_task_grant") or not request.get("task_id"):
                    return False
            request["state"] = "approved"
            request["approval_mode"] = "task" if mode == "task" else "once"
            request["event"].set()
            self._audit("approved", request, detail=request["approval_mode"])
            self._emit_state_locked("approved", request)
            return True

    def deny(self, request_id: str, *, reason: str = "user") -> bool:
        with self._lock:
            request = self._pending.get(str(request_id))
            self._expire_locked("denial_sweep")
            if not request or request.get("state") != "pending":
                return False
            self._mark_locked(request, "denied", detail=reason)
            return True

    def cancel_all(self, reason: str = "cancelled", *, shutdown: bool = False) -> int:
        count = 0
        with self._lock:
            self._expire_locked("cancel_sweep")
            self._shutting_down = self._shutting_down or bool(shutdown)
            for request in list(self._pending.values()):
                if request.get("state") in {"pending", "approved"} and int(request.get("executions") or 0) == 0:
                    self._mark_locked(request, "cancelled", detail=reason)
                    count += 1
            self._task_grants.clear()
        return count

    def consume(self, request_id: str, current: ActionContext) -> dict[str, Any]:
        with self._lock:
            request = self._pending.get(str(request_id))
            self._expire_locked("consume_sweep")
            if request and request.get("state") == "expired":
                return {"ok": False, "error": "authorization_expired"}
            if request and request.get("state") == "executed":
                return {"ok": False, "error": "authorization_already_consumed"}
            if not request or request.get("state") != "approved":
                return {"ok": False, "error": "authorization_not_approved"}
            checks = {
                "arguments_sha256": current.arguments_sha256,
                "context_sha256": current.digest,
                "owner_id": current.owner_id,
                "scope": current.scope,
                "session_id": current.session_id,
                "task_id": current.task_id or None,
            }
            if any(request.get(key) != value for key, value in checks.items()):
                self._mark_locked(request, "cancelled", detail="context_changed")
                return {"ok": False, "error": "authorization_context_changed"}
            if int(request.get("executions") or 0) != 0:
                return {"ok": False, "error": "authorization_already_consumed"}
            request["executions"] = 1
            if request.get("approval_mode") == "task" and request.get("allow_task_grant") and request.get("task_id"):
                self._task_grants.add((
                    str(request["task_id"]), str(request["owner_id"]), str(request["scope"]),
                    str(request["session_id"]), str(request["tool"]),
                ))
            self._mark_locked(request, "executed")
            return {"ok": True}

    def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        context: ActionContext,
        callback: Callable[[], Any],
        *,
        context_provider: Callable[[], ActionContext] | None = None,
    ) -> Any:
        decision = self.request(tool, arguments, context)
        if not decision.get("ok"):
            return decision
        request_id = decision.get("request_id")
        if request_id:
            current = context_provider() if callable(context_provider) else context
            consumed = self.consume(str(request_id), current)
            if not consumed.get("ok"):
                state = "expired" if consumed.get("error") == "authorization_expired" else "cancelled"
                return {"ok": False, "error": consumed.get("error"), "authorization_state": state}
        result = callback()
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("authorization_state", "approved")
        return result

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_locked("pending_query_sweep")
            return [self._public(row) for row in self._pending.values() if row.get("state") == "pending"]

    def request_state(self, request_id: str) -> str:
        with self._lock:
            self._expire_locked("state_query_sweep")
            row = self._pending.get(str(request_id))
            return str(row.get("state") or "missing") if row else "missing"
