from __future__ import annotations

"""Context snapshots used by Nova's local action authorization broker.

The snapshot contains only bounded metadata.  Full tool arguments, typed text,
commands and clipboard contents are deliberately excluded from persistence.
"""

from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
import unicodedata
from urllib.parse import urlsplit, urlunsplit
import uuid

from .action_apps import resolve_known_application
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


def _url_origin(value: str) -> str:
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return ""
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    except Exception:
        return ""


def _browser_observation(browser: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    observations: dict[str, Any] = {
        "browser_inspection": "unavailable",
        "browser_target_sha256": hashlib.sha256(
            str(arguments.get("target") or "").encode("utf-8", errors="surrogatepass")
        ).hexdigest(),
        "submit": bool(arguments.get("submit")) or str(arguments.get("key") or "").casefold() in {"enter", "return"},
    }
    if browser is None or not callable(getattr(browser, "call", None)):
        return observations
    target = str(arguments.get("target") or "")

    def capture_inside_worker():
        # Every Playwright access in this function is serialized by BrowserAgent.call().
        page = browser.page
        raw_url = str(page.url or "")
        snapshot: dict[str, Any] = {
            "raw_url": raw_url,
            "origin": _url_origin(raw_url),
        }
        if tool not in {"browser_click", "browser_fill"}:
            return snapshot
        resolver = getattr(browser, "resolve_with_selector", None)
        if callable(resolver):
            loc, resolved_selector = resolver(target)
        else:
            loc, resolved_selector = browser.resolve(target), target
        control = loc.evaluate("""el=>{
            const form=el.form||null;
            const attr=(name)=>String(el.getAttribute(name)||'');
            return {
                tag:String(el.tagName||'').toLowerCase(), id:attr('id'), name:attr('name'),
                role:attr('role'), aria_label:attr('aria-label'), type:attr('type'),
                effective_type:String(el.type||'').toLowerCase(), href:String(el.href||attr('href')),
                onclick:attr('onclick'), download:el.hasAttribute('download'),
                contenteditable:String(el.contentEditable||'').toLowerCase(),
                form_associated:!!form, form_id:form?String(form.id||''):'',
                form_name:form?String(form.name||''):'', form_method:form?String(form.method||'').toLowerCase():'',
                form_action:form?String(form.action||''):'', formaction:String(el.formAction||attr('formaction')),
                may_submit:!!form&&((String(el.tagName||'').toLowerCase()==='button'&&
                    (!String(el.type||'').toLowerCase()||String(el.type||'').toLowerCase()==='submit'))||
                    (String(el.tagName||'').toLowerCase()==='input'&&
                    ['submit','image'].includes(String(el.type||'').toLowerCase())))
            };
        }""")
        snapshot["resolved_selector"] = str(resolved_selector or "")
        snapshot["control"] = control
        return snapshot

    try:
        captured = browser.call(capture_inside_worker)
    except Exception as exc:
        observations["browser_inspection"] = "failed:" + type(exc).__name__
        return observations
    if not isinstance(captured, dict) or captured.get("ok") is False:
        observations["browser_inspection"] = "ambiguous"
        return observations

    raw_url = str(captured.get("raw_url") or "")
    observations.update({
        "browser_url": _safe_url(raw_url),
        "browser_full_url_sha256": hashlib.sha256(raw_url.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "browser_origin": str(captured.get("origin") or ""),
    })
    if tool not in {"browser_click", "browser_fill"}:
        observations["browser_inspection"] = "ok" if raw_url else "ambiguous"
        return observations

    raw = captured.get("control")
    if not isinstance(raw, dict) or not str(raw.get("tag") or ""):
        observations["browser_inspection"] = "ambiguous"
        return observations
    href = str(raw.get("href") or "")
    formaction = str(raw.get("formaction") or "")
    form_action = str(raw.get("form_action") or "")
    onclick = str(raw.get("onclick") or "")
    tag = str(raw.get("tag") or "").casefold()
    role = str(raw.get("role") or "").casefold()
    effective_type = str(raw.get("effective_type") or raw.get("type") or "").casefold()
    href_scheme = urlsplit(href).scheme.casefold() if href else ""
    form_associated = bool(raw.get("form_associated"))
    interactive_input = tag in {"input", "select", "textarea"} or str(raw.get("contenteditable") or "") == "true"
    passive_link = bool(
        tag == "a" and role != "button" and href_scheme in {"http", "https"}
        and not onclick and not bool(raw.get("download")) and not form_associated
        and not formaction and not interactive_input
    )
    selector = str(captured.get("resolved_selector") or "")
    control = {
        "tag": tag,
        "id": str(raw.get("id") or "")[:240],
        "name": str(raw.get("name") or "")[:240],
        "role": role[:80],
        "aria-label": str(raw.get("aria_label") or "")[:240],
        "type": str(raw.get("type") or "")[:80],
        "effective_type": effective_type[:80],
        "href": _safe_url(href),
        "href_sha256": hashlib.sha256(href.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "href_scheme": href_scheme,
        "onclick": bool(onclick),
        "onclick_sha256": hashlib.sha256(onclick.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "download": bool(raw.get("download")),
        "form_associated": form_associated,
        "form_id": str(raw.get("form_id") or "")[:160],
        "form_name": str(raw.get("form_name") or "")[:160],
        "form_method": str(raw.get("form_method") or "")[:40],
        "form_action": _safe_url(form_action),
        "form_action_sha256": hashlib.sha256(form_action.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "formaction": _safe_url(formaction),
        "formaction_sha256": hashlib.sha256(formaction.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "may_submit": bool(raw.get("may_submit")),
        "interactive_input": interactive_input,
        "passive_link": passive_link,
        "unsafe_destination": bool(href and href_scheme not in {"http", "https"}),
        "resolved_selector": selector[:240],
        "resolved_selector_sha256": hashlib.sha256(selector.encode("utf-8", errors="surrogatepass")).hexdigest(),
        "element_identity_sha256": sha256_json(raw),
    }
    observations["browser_control"] = control
    observations["browser_inspection"] = "ok"
    return observations


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


_POSITIVE_INTENT = {
    "clipboard_read": (
        re.compile(r"\b(?:lee|leer|revisa|revisar|muestra|mostrar)\s+(?:(?:mi|el)\s+)?(?:portapapeles|clipboard)\b"),
    ),
    "screenshot": (
        re.compile(r"\b(?:haz|hacer|toma|tomar|saca|sacar|realiza|realizar)\s+(?:una\s+)?(?:captura|screenshot)(?:\s+de\s+(?:mi|la)\s+pantalla)?\b"),
    ),
    "vision_describe_screen": (
        re.compile(r"\b(?:describe|describir|mira|mirar|analiza|analizar|observa|observar)\s+(?:lo\s+que\s+(?:aparece|hay|se\s+ve)\s+en\s+)?(?:mi|la)\s+pantalla\b"),
    ),
    "expert_import_chatgpt_response": (
        re.compile(r"\b(?:importa|importar|carga|cargar|usa|usar)\s+(?:esta|la|mi)\s+respuesta\s+(?:de\s+)?chatgpt\b"),
    ),
}
_NEGATED_ACTION = re.compile(
    r"\b(?:no|nunca|jamas|evita|evitar|sin)\b[^.!?;\n]{0,100}"
    r"\b(?:lee|leer|revisa|muestra|captura|screenshot|describe|mira|analiza|observa|importa|carga|usa)\b"
)
_QUOTED = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’", flags=re.S)
_EXAMPLE_CONTEXT = re.compile(r"\b(?:ejemplo|cita|texto\s+remoto|archivo|web|commit|planner)\s*(?::|dice\b)")
_REPORTED_CONTEXT = re.compile(
    r"\b(?:web|pagina|sitio|archivo|documento|commit|release|planner|modelo|memoria|skill|"
    r"texto\s+remoto|contenido\s+remoto)\b[^.!?;\n]{0,160}"
    r"\b(?:lee|leer|revisa|revisar|muestra|mostrar|haz|hacer|toma|tomar|saca|sacar|"
    r"describe|describir|mira|mirar|analiza|analizar|observa|observar|importa|importar|carga|cargar|usa|usar)\b"
)


@dataclass(frozen=True)
class HumanIntent:
    """Hashed capability derived only from the original local human order."""

    source: str
    text_sha256: str
    session_id: str
    request_id: str
    sensitive_tools: frozenset[str] = field(default_factory=frozenset)


_CURRENT_HUMAN_INTENT: ContextVar[HumanIntent | None] = ContextVar("nova_current_human_intent", default=None)


def _intent_text(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"(?m)^\s*>.*$", " ", raw)
    raw = _QUOTED.sub(" ", raw)
    return " ".join(raw.split())


def human_intent_from_text(
    text: str,
    *,
    source: str = "local_user",
    session_id: str = "",
    request_id: str = "",
) -> HumanIntent:
    raw = str(text or "")
    normalized = _intent_text(raw)
    source_name = str(source or "")
    allowed: frozenset[str] = frozenset()
    if (
        source_name == "local_user"
        and not _NEGATED_ACTION.search(normalized)
        and not _EXAMPLE_CONTEXT.search(normalized)
        and not _REPORTED_CONTEXT.search(normalized)
    ):
        allowed = frozenset(
            tool for tool, rules in _POSITIVE_INTENT.items()
            if any(rule.search(normalized) for rule in rules)
        )
    return HumanIntent(
        source=source_name,
        text_sha256=hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest(),
        session_id=str(session_id or ""),
        request_id=str(request_id or uuid.uuid4().hex),
        sensitive_tools=allowed,
    )


def explicit_intent_for(tool: str, intent: HumanIntent | None, *, session_id: str) -> bool:
    return bool(
        isinstance(intent, HumanIntent)
        and intent.source == "local_user"
        and str(tool or "") in intent.sensitive_tools
        and len(intent.text_sha256) == 64
        and intent.session_id == str(session_id or "")
        and bool(re.fullmatch(r"[0-9a-f]{32}", intent.request_id))
    )


def current_human_intent() -> HumanIntent | None:
    return _CURRENT_HUMAN_INTENT.get()


@contextmanager
def bind_human_intent(intent: HumanIntent | None):
    safe = intent if isinstance(intent, HumanIntent) and intent.source == "local_user" else None
    token = _CURRENT_HUMAN_INTENT.set(safe)
    try:
        yield safe
    finally:
        _CURRENT_HUMAN_INTENT.reset(token)


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
    intent_session_id: str = ""
    intent_request_id: str = ""
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
            "intent_session_id": self.intent_session_id,
            "intent_request_id": self.intent_request_id,
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
    selected_intent = human_intent if isinstance(human_intent, HumanIntent) else current_human_intent()
    observations: dict[str, Any] = {}

    if "path" in args and tools is not None:
        try:
            target_path = tools._ensure_allowed(tools._resolve_path(args.get("path")))
            observations["file"] = _file_observation(target_path)
            observations["path_sha256"] = hashlib.sha256(str(target_path).encode("utf-8", errors="ignore")).hexdigest()
        except Exception as exc:
            observations["file_error"] = type(exc).__name__

    if str(tool) in {"browser_click", "browser_fill", "browser_press"}:
        observations.update(_browser_observation(getattr(tools, "browser_agent", None), str(tool), args))

    if str(tool) == "open_app":
        resolved = resolve_known_application(str(args.get("app") or ""))
        observations["application"] = {
            "allowed": resolved.allowed,
            "kind": resolved.kind,
            "alias": resolved.display_name,
            "path_sha256": hashlib.sha256(str(resolved.path or "").encode("utf-8", errors="ignore")).hexdigest(),
            "file": _file_observation(resolved.path) if resolved.path is not None else {"exists": False, "kind": "missing"},
        }

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
        explicit_intent=explicit_intent_for(tool, selected_intent, session_id=selected_session),
        intent_sha256=selected_intent.text_sha256 if isinstance(selected_intent, HumanIntent) else "",
        intent_source=selected_intent.source if isinstance(selected_intent, HumanIntent) else "",
        intent_session_id=selected_intent.session_id if isinstance(selected_intent, HumanIntent) else "",
        intent_request_id=selected_intent.request_id if isinstance(selected_intent, HumanIntent) else "",
        observations=observations,
    )


def redacted_argument_shape(arguments: dict[str, Any] | None) -> dict[str, str]:
    """Useful for diagnostics without ever exposing argument values."""
    result = {}
    for key, value in dict(arguments or {}).items():
        result[str(key)] = "secret" if str(key).casefold() in _SECRET_KEYS else type(value).__name__
    return result
