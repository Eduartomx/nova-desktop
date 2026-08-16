from __future__ import annotations

"""Single local authority for permissioned tool execution."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any, Callable

from .action_context import ActionContext


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
    "open_app", "open_url", "browser_open", "browser_search", "browser_back", "browser_reload",
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

_FORBIDDEN_PS = re.compile(
    r"(?is)(remove-item\b.*(?:-recurse|-force)|\b(?:del|erase|rd|rmdir)\b.*(?:/s|/q)|"
    r"format-(?:volume|disk)\b|clear-disk\b|initialize-disk\b|bcdedit\b|bootrec\b|"
    r"stop-computer\b|restart-computer\b|shutdown(?:\.exe)?\b|reg(?:\.exe)?\s+delete\b|"
    r"set-executionpolicy\b|disable-(?:windowsoptionalfeature|computerrestore|bitlocker)\b|"
    r"set-mppreference\b.*disable|disable.*(?:defender|firewall|antivirus|security)|"
    r"credential|sam\\|security\\policy|invoke-expression\b|\biex\b|downloadstring\b|frombase64string\b)"
)
_SENSITIVE_TARGET = re.compile(
    r"(?i)\b(buy|purchase|checkout|pay|transfer|send|post|publish|delete|remove|"
    r"submit|comprar|pagar|transferir|enviar|publicar|eliminar|borrar|confirmar|pedido)\b"
)


def policy_for(tool: str, arguments: dict[str, Any] | None = None, *, known_tools: set[str] | None = None) -> ActionPolicy:
    name = str(tool or "")
    args = dict(arguments or {})
    if known_tools is not None and name not in known_tools:
        return ActionPolicy("forbidden", "critical", "Herramienta desconocida: política fail-closed.")
    if name == "powershell" and _FORBIDDEN_PS.search(str(args.get("command") or "")):
        return ActionPolicy("forbidden", "critical", "Comando de borrado, arranque, seguridad o credenciales prohibido.")
    if name == "browser_fill" and bool(args.get("submit")):
        return ActionPolicy("high_risk", "high", "Rellenar y enviar un formulario requiere permiso de una sola vez.")
    if name in {"browser_click", "uia_click"} and _SENSITIVE_TARGET.search(str(args.get("target") or args.get("control") or "")):
        return ActionPolicy("high_risk", "high", "El control puede producir un efecto externo sensible.")
    if name in _READ_ONLY:
        return ActionPolicy("read_only", "low", "Lectura local o pública sin efectos.")
    if name in _SENSITIVE_READ:
        return ActionPolicy("sensitive_read", "medium", "Lectura sensible solicitada explícitamente.")
    if name in _REVERSIBLE:
        return ActionPolicy("reversible", "low", "Efecto local reversible.", allow_task_grant=True)
    if name in _MUTATING:
        return ActionPolicy("mutating", "medium", "La acción modifica estado.", allow_task_grant=True)
    if name in _HIGH_RISK:
        return ActionPolicy("high_risk", "high", "Acción de alto riesgo; permiso de una sola vez.")
    # Every schema receives a policy, but unclassified tools are conservative.
    if known_tools is not None and name in known_tools:
        return ActionPolicy("mutating", "medium", "Herramienta registrada sin clasificación específica.", allow_task_grant=True)
    return ActionPolicy("forbidden", "critical", "Herramienta desconocida: política fail-closed.")


def policy_registry(tool_names: set[str] | list[str]) -> dict[str, ActionPolicy]:
    names = {str(name) for name in tool_names if str(name)}
    return {name: policy_for(name, {}, known_tools=names) for name in names}


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
        self._task_grants: set[tuple[str, str, str, str]] = set()
        self._approval_handler: Callable[[dict[str, Any]], Any] | None = None
        self._shutting_down = False
        self.total_wait_seconds = 0.0

    @property
    def profile(self) -> str:
        security = self.config.get("security", {}) if isinstance(self.config, dict) else {}
        value = str(security.get("profile") or "balanced").casefold()
        return value if value in {"safe", "balanced", "trusted"} else "balanced"

    def set_approval_handler(self, handler: Callable[[dict[str, Any]], Any] | None) -> None:
        with self._lock:
            self._approval_handler = handler

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

    def _audit(self, event: str, request: dict[str, Any], *, detail: str = "") -> None:
        if self.audit_path is None:
            return
        payload = {
            "timestamp": _now_iso(), "event": str(event), "request_id": request.get("request_id"),
            "tool": request.get("tool"), "effect": request.get("effect"), "risk": request.get("risk"),
            "target_sha256": hashlib.sha256(str(request.get("target") or "").encode("utf-8", errors="ignore")).hexdigest(),
            "task_id": request.get("task_id"),
            "owner_id": request.get("owner_id"), "scope": request.get("scope"),
            "arguments_sha256": request.get("arguments_sha256"), "state": request.get("state"),
            "detail": str(detail or "")[:160],
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _public(self, request: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "request_id", "tool", "effect", "risk", "target", "reason", "task_id", "owner_id",
            "scope", "session_id", "created_at", "expires_at", "state", "allow_task_grant",
        )
        return {key: request.get(key) for key in keys}

    def request(self, tool: str, arguments: dict[str, Any], context: ActionContext, *, timeout=None) -> dict[str, Any]:
        policy = policy_for(tool, arguments, known_tools=self.known_tools)
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
        if policy.effect == "sensitive_read" and not context.explicit_intent:
            return {"ok": False, "error": "explicit_intent_required", "authorization_state": "denied"}
        if policy.effect == "read_only" or not self._requires_approval(policy):
            return {"ok": True, "authorization_state": "approved", "automatic": True, "policy": policy}

        # A headless Task Engine may have yielded while pending. If the local UI
        # approved that exact immutable context afterwards, the resumed step may
        # claim it once; a changed context cannot reuse it.
        with self._lock:
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

        grant_key = (context.task_id, context.owner_id, context.scope, str(tool))
        if policy.allow_task_grant and context.task_id and grant_key in self._task_grants:
            return {"ok": True, "authorization_state": "approved", "automatic": True, "task_grant": True, "policy": policy}

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
            if self._shutting_down:
                return {"ok": False, "error": "authorization_cancelled", "authorization_state": "cancelled"}
            self._pending[request["request_id"]] = request
            handler = self._approval_handler
        self._audit("pending", request)
        if handler is None:
            return {"ok": False, "error": "waiting_for_approval", "authorization_state": "pending", "request": self._public(request)}
        try:
            handler(self._public(request))
        except Exception:
            self.deny(request["request_id"], reason="approval_ui_error")

        wait_started = self._clock()
        event.wait(wait_seconds)
        waited = max(0.0, self._clock() - wait_started)
        with self._lock:
            self.total_wait_seconds += waited
            if request["state"] == "pending":
                request["state"] = "expired"
                self._audit("expired", request)
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
            if not request or request.get("state") != "pending" or self._shutting_down:
                return False
            if self._clock() >= float(request.get("expires_at") or 0):
                request["state"] = "expired"
                request["event"].set()
                self._audit("expired", request)
                return False
            if mode == "task":
                if not request.get("allow_task_grant") or not request.get("task_id"):
                    return False
                self._task_grants.add((str(request["task_id"]), str(request["owner_id"]), str(request["scope"]), str(request["tool"])))
            request["state"] = "approved"
            request["approval_mode"] = "task" if mode == "task" else "once"
            request["event"].set()
            self._audit("approved", request, detail=request["approval_mode"])
            return True

    def deny(self, request_id: str, *, reason: str = "user") -> bool:
        with self._lock:
            request = self._pending.get(str(request_id))
            if not request or request.get("state") != "pending":
                return False
            request["state"] = "denied"
            request["event"].set()
            self._audit("denied", request, detail=reason)
            return True

    def cancel_all(self, reason: str = "cancelled", *, shutdown: bool = False) -> int:
        count = 0
        with self._lock:
            self._shutting_down = self._shutting_down or bool(shutdown)
            for request in self._pending.values():
                if request.get("state") == "pending":
                    request["state"] = "cancelled"
                    request["event"].set()
                    self._audit("cancelled", request, detail=reason)
                    count += 1
            self._task_grants.clear()
        return count

    def consume(self, request_id: str, current: ActionContext) -> dict[str, Any]:
        with self._lock:
            request = self._pending.get(str(request_id))
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
                request["state"] = "cancelled"
                self._audit("cancelled", request, detail="context_changed")
                return {"ok": False, "error": "authorization_context_changed"}
            if int(request.get("executions") or 0) != 0:
                return {"ok": False, "error": "authorization_already_consumed"}
            request["executions"] = 1
            request["state"] = "executed"
            self._audit("executed", request)
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
                return {"ok": False, "error": consumed.get("error"), "authorization_state": "cancelled"}
        result = callback()
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("authorization_state", "approved")
        return result

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(row) for row in self._pending.values() if row.get("state") == "pending"]
