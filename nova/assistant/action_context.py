from __future__ import annotations

"""Context snapshots used by Nova's local action authorization broker.

The snapshot contains only bounded metadata.  Full tool arguments, typed text,
commands and clipboard contents are deliberately excluded from persistence.
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .action_powershell import classify_powershell


_SECRET_KEYS = {
    "content", "text", "command", "password", "token", "secret", "cookie",
    "authorization", "api_key", "response", "problem", "local_answer",
}


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def sha256_json(value: Any) -> str:
    raw = json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()


def arguments_hash(tool: str, arguments: dict[str, Any] | None) -> str:
    """Bind a permission to the exact tool and arguments without logging them."""
    return sha256_json({"tool": str(tool or ""), "arguments": dict(arguments or {})})


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in {"http", "https"}:
            return ""
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path[:500], "", ""))
    except Exception:
        return ""


def _file_observation(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"exists": path.exists(), "kind": "missing"}
    if not path.exists():
        return result
    try:
        stat = path.stat()
        result.update({
            "kind": "dir" if path.is_dir() else "file",
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        })
        if path.is_file() and stat.st_size <= 2 * 1024 * 1024:
            result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        result["error"] = type(exc).__name__
    return result


def _sanitized_target(tool: str, arguments: dict[str, Any]) -> str:
    if str(tool) == "powershell":
        return classify_powershell(str(arguments.get("command") or "")).target[:180]
    for key in ("path", "url", "app", "title", "window", "control", "target", "workspace", "skill"):
        if key not in arguments:
            continue
        raw = str(arguments.get(key) or "")
        if key == "url":
            return _safe_url(raw)[:600]
        if key == "path":
            path = Path(os.path.expandvars(os.path.expanduser(raw)))
            return path.name[:180] or "."
        return raw.replace("\r", " ").replace("\n", " ")[:180]
    return str(tool or "")[:180]


_INTENT_CUES = {
    "clipboard_read": ("portapapeles", "clipboard"),
    "screenshot": ("captura", "screenshot", "pantalla"),
    "vision_describe_screen": ("mira", "captura", "pantalla", "describe"),
    "expert_import_chatgpt_response": ("importa", "respuesta", "portapapeles"),
}


@dataclass(frozen=True)
class HumanIntent:
    """Hashed capability derived only from the original local human order."""

    source: str
    text_sha256: str
    sensitive_tools: frozenset[str] = field(default_factory=frozenset)


def human_intent_from_text(text: str, *, source: str = "local_user") -> HumanIntent:
    raw = str(text or "")
    normalized = raw.casefold()
    source_name = str(source or "")
    allowed = frozenset(
        tool for tool, cues in _INTENT_CUES.items()
        if source_name == "local_user" and any(cue in normalized for cue in cues)
    )
    return HumanIntent(
        source=source_name,
        text_sha256=hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest(),
        sensitive_tools=allowed,
    )


def explicit_intent_for(tool: str, intent: HumanIntent | None) -> bool:
    return bool(
        isinstance(intent, HumanIntent)
        and intent.source == "local_user"
        and str(tool or "") in intent.sensitive_tools
        and len(intent.text_sha256) == 64
    )


@dataclass(frozen=True)
class ActionContext:
    tool: str
    arguments_sha256: str
    owner_id: str
    scope: str
    session_id: str
    task_id: str = ""
    target: str = ""
    explicit_intent: bool = False
    intent_sha256: str = ""
    intent_source: str = ""
    observations: dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return sha256_json({
            "tool": self.tool,
            "arguments_sha256": self.arguments_sha256,
            "owner_id": self.owner_id,
            "scope": self.scope,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "target": self.target,
            "explicit_intent": self.explicit_intent,
            "intent_sha256": self.intent_sha256,
            "intent_source": self.intent_source,
            "observations": self.observations,
        })

    def public_metadata(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "owner_id": self.owner_id,
            "scope": self.scope,
            "session_id": self.session_id,
            "task_id": self.task_id or None,
            "target": self.target,
            "arguments_sha256": self.arguments_sha256,
            "context_sha256": self.digest,
        }


def build_action_context(
    tool: str,
    arguments: dict[str, Any] | None,
    *,
    tools: Any = None,
    task_id: Any = None,
    human_intent: HumanIntent | None = None,
    user_text: str = "",
    owner_id: str = "",
    scope: str = "",
    session_id: str = "",
) -> ActionContext:
    args = dict(arguments or {})
    owner = str(owner_id or getattr(tools, "action_owner_id", "") or "local-runtime")
    selected_scope = str(scope or getattr(tools, "action_scope", "") or "local-ui")
    selected_session = str(session_id or getattr(tools, "action_session_id", "") or os.getpid())
    selected_task = str(task_id if task_id is not None else getattr(tools, "action_task_id", "") or "")
    observations: dict[str, Any] = {}

    if "path" in args and tools is not None:
        try:
            target_path = tools._ensure_allowed(tools._resolve_path(args.get("path")))
            observations["file"] = _file_observation(target_path)
            observations["path_sha256"] = hashlib.sha256(str(target_path).encode("utf-8", errors="ignore")).hexdigest()
        except Exception as exc:
            observations["file_error"] = type(exc).__name__

    browser = getattr(tools, "browser_agent", None)
    page = getattr(browser, "_page", None)
    if str(tool).startswith("browser_"):
        observations["browser_inspection"] = "unavailable"
    if str(tool).startswith("browser_") and page is not None:
        try:
            observations["browser_url"] = _safe_url(str(page.url))
        except Exception:
            observations["browser_url"] = ""
        observations["browser_target"] = str(args.get("target") or "")[:240]
        observations["submit"] = bool(args.get("submit")) or str(args.get("key") or "").casefold() in {"enter", "return"}
        if str(tool) in {"browser_click", "browser_fill"} and callable(getattr(browser, "call", None)):
            target = str(args.get("target") or "")
            def inspect_target():
                loc = browser.resolve(target)
                attrs = {}
                for name in ("id", "name", "type", "role", "aria-label", "formaction", "formmethod", "form", "href"):
                    try:
                        attrs[name] = str(loc.get_attribute(name) or "")[:240]
                    except Exception:
                        attrs[name] = ""
                try:
                    attrs["tag"] = str(loc.evaluate("el=>el.tagName.toLowerCase()") or "")[:40]
                except Exception:
                    attrs["tag"] = ""
                try:
                    attrs["effective_type"] = str(loc.evaluate("el=>String(el.type||'').toLowerCase()") or "")[:40]
                except Exception:
                    attrs["effective_type"] = ""
                try:
                    attrs["form_associated"] = bool(loc.evaluate("el=>!!el.form"))
                    attrs["may_submit"] = bool(loc.evaluate(
                        "el=>{const t=el.tagName.toLowerCase();const k=String(el.type||'').toLowerCase();"
                        "return !!el.form&&((t==='button'&&(!k||k==='submit'))||(t==='input'&&(k==='submit'||k==='image')))}"
                    ))
                except Exception:
                    attrs["form_associated"] = None
                    attrs["may_submit"] = None
                return attrs
            try:
                inspected = browser.call(inspect_target)
                if isinstance(inspected, dict) and inspected.get("ok") is not False and inspected.get("tag"):
                    observations["browser_control"] = inspected
                    observations["browser_inspection"] = "ok"
                else:
                    observations["browser_inspection"] = "ambiguous"
            except Exception as exc:
                observations["browser_inspection"] = "failed:" + type(exc).__name__

    if str(tool).startswith(("window_", "uia_", "mouse_", "keyboard_")):
        observations["window"] = str(args.get("window") or args.get("title") or "")[:240]
        observations["control"] = str(args.get("control") or args.get("target") or "")[:240]
        refs = getattr(tools, "_uia_refs", {})
        raw_control = str(args.get("control") or "")
        try:
            ref_number = int(re.sub(r"(?i)^(?:ref\s*[=:]?\s*|#)", "", raw_control))
        except Exception:
            ref_number = 0
        if ref_number and isinstance(refs, dict) and isinstance(refs.get(ref_number), dict):
            observations["uia_ref"] = dict(refs[ref_number])
        window_list = getattr(tools, "window_list", None)
        if callable(window_list) and str(tool).startswith(("window_", "uia_")):
            try:
                snapshot = window_list()
                title = observations["window"].casefold()
                rows = snapshot.get("windows", []) if isinstance(snapshot, dict) else []
                observations["window_matches"] = [
                    {"title": str(row.get("title") or "")[:240], "handle": int(row.get("handle") or 0), "rect": row.get("rect")}
                    for row in rows if not title or title in str(row.get("title") or "").casefold()
                ][:4]
            except Exception:
                observations["window_matches"] = []

    return ActionContext(
        tool=str(tool or ""),
        arguments_sha256=arguments_hash(tool, args),
        owner_id=owner,
        scope=selected_scope,
        session_id=selected_session,
        task_id=selected_task,
        target=_sanitized_target(tool, args),
        explicit_intent=explicit_intent_for(tool, human_intent),
        intent_sha256=human_intent.text_sha256 if isinstance(human_intent, HumanIntent) else "",
        intent_source=human_intent.source if isinstance(human_intent, HumanIntent) else "",
        observations=observations,
    )


def redacted_argument_shape(arguments: dict[str, Any] | None) -> dict[str, str]:
    """Useful for diagnostics without ever exposing argument values."""
    result = {}
    for key, value in dict(arguments or {}).items():
        result[str(key)] = "secret" if str(key).casefold() in _SECRET_KEYS else type(value).__name__
    return result
