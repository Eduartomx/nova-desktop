from __future__ import annotations

"""Capacidades de escritorio/navegador administradas por GitHub.

0.9.0 preserva Browser Agent, ventanas/UI Automation y entrada sintética sin
volver a depender del antiguo tools.py local. Playwright vive en un hilo
dedicado porque su API sync no debe reutilizar objetos desde hilos distintos.
"""

import atexit
import queue
import re
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus


def _fn(name: str, description: str, properties=None, required=None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


BROWSER_SCHEMAS = [
    _fn("browser_open", "Abre una URL en el Browser Agent persistente de Nova.", {"url": {"type": "string"}}, ["url"]),
    _fn("browser_search", "Busca en Internet usando el Browser Agent.", {"query": {"type": "string"}}, ["query"]),
    _fn("browser_read", "Lee texto visible de la pestaña actual.", {"max_chars": {"type": "integer"}}),
    _fn("browser_inspect", "Inspecciona controles interactivos de la página y devuelve referencias numeradas."),
    _fn("browser_click", "Hace click en una referencia/selector/texto visible. Las acciones sensibles se bloquean para confirmación.", {"target": {"type": "string"}}, ["target"]),
    _fn("browser_fill", "Rellena un control por referencia/selector/texto. No envía formularios salvo submit=true.", {"target": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["target", "text"]),
    _fn("browser_press", "Envía una tecla a la página actual.", {"key": {"type": "string"}}, ["key"]),
    _fn("browser_tabs", "Lista las pestañas del Browser Agent y la activa.", {"activate": {"type": "integer"}}),
    _fn("browser_back", "Navega atrás en la pestaña actual."),
    _fn("browser_reload", "Recarga la pestaña actual."),
]

DESKTOP_SCHEMAS = [
    _fn("window_list", "Lista ventanas de escritorio visibles."),
    _fn("window_activate", "Activa una ventana por título/fragmento.", {"title": {"type": "string"}}, ["title"]),
    _fn("window_close", "Cierra una ventana. Puede requerir confirmación según seguridad.", {"title": {"type": "string"}}, ["title"]),
    _fn("window_move", "Mueve/redimensiona una ventana.", {"title": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"}}, ["title", "x", "y", "width", "height"]),
    _fn("uia_snapshot", "Obtiene controles UI Automation de una ventana, sin screenshot.", {"title": {"type": "string"}, "limit": {"type": "integer"}}),
    _fn("uia_click", "Invoca/clica un control UI Automation por nombre o auto_id.", {"window": {"type": "string"}, "control": {"type": "string"}}, ["control"]),
    _fn("uia_type", "Escribe en un control UI Automation identificado.", {"window": {"type": "string"}, "control": {"type": "string"}, "text": {"type": "string"}}, ["control", "text"]),
    _fn("mouse_move", "Mueve el puntero a coordenadas absolutas.", {"x": {"type": "integer"}, "y": {"type": "integer"}}, ["x", "y"]),
    _fn("mouse_click", "Hace click en coordenadas absolutas.", {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "count": {"type": "integer"}}, ["x", "y"]),
    _fn("keyboard_type", "Escribe texto mediante teclado sintético.", {"text": {"type": "string"}}, ["text"]),
    _fn("keyboard_press", "Pulsa una tecla o combinación sencilla.", {"key": {"type": "string"}}, ["key"]),
]


_SENSITIVE_BROWSER = re.compile(
    r"(?i)\b(buy|purchase|checkout|pay|payment|transfer|send|post|publish|delete|remove|"
    r"comprar|pagar|pago|transferir|enviar|publicar|eliminar|borrar|confirm order|place order)\b"
)


class _BrowserWorker:
    def __init__(self, config: dict[str, Any], data_dir: Path):
        self.config = config
        self.data_dir = Path(data_dir)
        self._requests: queue.Queue[tuple[Callable[[], Any], queue.Queue]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopping = False
        self._playwright = None
        self._context = None
        self._page = None
        self._refs: dict[int, str] = {}

    def _ensure_thread(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._loop, daemon=True, name="nova-browser-worker")
            self._thread.start()

    def call(self, fn: Callable[[], Any], timeout: float | None = None):
        self._ensure_thread()
        result_q: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put((fn, result_q))
        wait = float(timeout or self.config.get("browser", {}).get("command_timeout_seconds", 35) or 35) + 5.0
        try:
            ok, value = result_q.get(timeout=wait)
        except queue.Empty:
            return {"ok": False, "error": "browser_worker_timeout"}
        if ok:
            return value
        return {"ok": False, "error": type(value).__name__, "detail": str(value)[:900]}

    def _loop(self):
        try:
            while not self._stopping:
                try:
                    fn, result_q = self._requests.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    self._ensure_browser()
                    result_q.put((True, fn()))
                except Exception as exc:
                    result_q.put((False, exc))
        finally:
            self._close_browser()

    def _ensure_browser(self):
        if self._context is not None:
            try:
                pages = list(self._context.pages)
                self._page = self._page if self._page in pages else (pages[-1] if pages else self._context.new_page())
                return
            except Exception:
                self._close_browser()
        from playwright.sync_api import sync_playwright
        self.data_dir.mkdir(parents=True, exist_ok=True)
        profile = self.data_dir / "browser_profile"
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        cfg = self.config.get("browser", {}) if isinstance(self.config, dict) else {}
        kwargs = {
            "user_data_dir": str(profile),
            "headless": bool(cfg.get("headless", False)),
            "viewport": None,
        }
        channel = str(cfg.get("channel") or "msedge").strip()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(channel=channel, **kwargs)
        except Exception:
            self._context = self._playwright.chromium.launch_persistent_context(**kwargs)
        self._context.set_default_timeout(int(cfg.get("action_timeout_ms", 9000) or 9000))
        self._context.set_default_navigation_timeout(int(cfg.get("navigation_timeout_ms", 25000) or 25000))
        pages = list(self._context.pages)
        self._page = pages[-1] if pages else self._context.new_page()

    def _close_browser(self):
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._playwright = None
        self._page = None
        self._refs = {}

    def stop(self):
        self._stopping = True

    @property
    def page(self):
        self._ensure_browser()
        return self._page

    def resolve(self, target: str):
        page = self.page
        raw = str(target or "").strip()
        match = re.fullmatch(r"(?:ref\s*[=:]?\s*|#)?(\d+)", raw, flags=re.I)
        if match:
            selector = self._refs.get(int(match.group(1)))
            if not selector:
                raise ValueError(f"Referencia de navegador no vigente: {raw}")
            return page.locator(selector).first
        if raw.startswith(("css=", "xpath=", "text=", "role=")):
            return page.locator(raw).first
        # Intenta etiqueta/nombre accesible y luego texto.
        for role in ("button", "link", "textbox", "checkbox", "radio", "combobox", "menuitem", "tab"):
            try:
                loc = page.get_by_role(role, name=raw, exact=False)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass
        try:
            loc = page.get_by_text(raw, exact=False)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
        return page.locator(raw).first

    def inspect(self):
        page = self.page
        self._refs = {}
        selectors = "a,button,input,textarea,select,[role=button],[role=link],[role=textbox],[contenteditable=true]"
        loc = page.locator(selectors)
        count = min(loc.count(), 180)
        rows = []
        for i in range(count):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                nova_id = f"nova-ref-{len(rows)+1}"
                item.evaluate("(el,id)=>el.setAttribute('data-nova-ref',id)", nova_id)
                ref = len(rows) + 1
                self._refs[ref] = f'[data-nova-ref="{nova_id}"]'
                tag = item.evaluate("el=>el.tagName.toLowerCase()")
                role = item.get_attribute("role") or ""
                name = (item.get_attribute("aria-label") or item.get_attribute("name") or item.get_attribute("placeholder") or item.inner_text() or "").strip()
                typ = item.get_attribute("type") or ""
                rows.append({"ref": ref, "tag": tag, "role": role, "type": typ, "name": name[:240]})
            except Exception:
                continue
        return {"ok": True, "url": page.url, "title": page.title(), "elements": rows, "count": len(rows)}


_browser_instances: dict[str, _BrowserWorker] = {}
_browser_lock = threading.Lock()


def _browser_worker(config: dict[str, Any], data_dir: Path) -> _BrowserWorker:
    key = str(Path(data_dir).resolve())
    with _browser_lock:
        worker = _browser_instances.get(key)
        if worker is None:
            worker = _BrowserWorker(config, data_dir)
            _browser_instances[key] = worker
        else:
            worker.config = config
        return worker


def _stop_all():
    for worker in list(_browser_instances.values()):
        try:
            worker.stop()
        except Exception:
            pass


atexit.register(_stop_all)


def install_tools_desktop():
    from . import tools as mod

    existing = {x.get("function", {}).get("name") for x in mod.TOOL_SCHEMAS}
    for schema in BROWSER_SCHEMAS + DESKTOP_SCHEMAS:
        if schema["function"]["name"] not in existing:
            mod.TOOL_SCHEMAS.append(schema)

    LocalTools = mod.LocalTools
    if not getattr(LocalTools, "_nova_desktop_core", False):
        original_init = LocalTools.__init__

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            data_dir = Path(__file__).resolve().parent.parent / "data"
            self.browser_agent = _browser_worker(self.config, data_dir)
            self._uia_refs: dict[int, dict[str, str]] = {}

        # ---------- Browser Agent ----------
        def browser_open(self, url):
            raw = str(url or "").strip()
            if not re.match(r"^https?://", raw, flags=re.I):
                raw = "https://" + raw
            def op():
                page = self.browser_agent.page
                page.goto(raw, wait_until="domcontentloaded")
                return {"ok": True, "url": page.url, "title": page.title()}
            return self.browser_agent.call(op)

        def browser_search(self, query):
            q = str(query or "").strip()
            engine = str(self.config.get("browser", {}).get("search_engine", "google")).casefold()
            if engine == "duckduckgo":
                url = "https://duckduckgo.com/?q=" + quote_plus(q)
            else:
                url = "https://www.google.com/search?q=" + quote_plus(q)
            return browser_open(self, url)

        def browser_read(self, max_chars=18000):
            max_chars = max(500, min(int(max_chars or 18000), 80000))
            def op():
                page = self.browser_agent.page
                text = page.locator("body").inner_text(timeout=int(self.config.get("browser", {}).get("action_timeout_ms", 9000)))
                return {"ok": True, "url": page.url, "title": page.title(), "text": text[:max_chars], "truncated": len(text) > max_chars}
            return self.browser_agent.call(op)

        def browser_inspect(self):
            return self.browser_agent.call(lambda: self.browser_agent.inspect())

        def browser_click(self, target):
            raw = str(target or "").strip()
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_browser_sensitive_clicks", True) and _SENSITIVE_BROWSER.search(raw):
                return {"ok": False, "error": "confirmation_required", "detail": "El objetivo parece una acción web sensible; requiere confirmación explícita."}
            def op():
                page = self.browser_agent.page
                loc = self.browser_agent.resolve(raw)
                try:
                    label = (loc.get_attribute("aria-label") or loc.inner_text() or "")[:300]
                except Exception:
                    label = raw
                if not getattr(self, "_action_broker_executing", False):
                    try:
                        tag = str(loc.evaluate("el=>el.tagName.toLowerCase()") or "").casefold()
                        typ = str(loc.evaluate("el=>String(el.type||'').toLowerCase()") or "").casefold()
                        associated = bool(loc.evaluate("el=>!!el.form"))
                        formaction = str(loc.get_attribute("formaction") or "")
                        submits = bool(formaction) or typ in {"submit", "image"} or (tag == "button" and associated and typ in {"", "submit"})
                    except Exception:
                        return {"ok": False, "error": "confirmation_required", "detail": "No fue posible clasificar el control web de forma segura."}
                    if submits or (self.config.get("security", {}).get("confirm_browser_sensitive_clicks", True) and _SENSITIVE_BROWSER.search(label)):
                        return {"ok": False, "error": "confirmation_required", "detail": "El control puede producir un envío o efecto externo."}
                loc.click()
                return {"ok": True, "clicked": label or raw, "url": page.url}
            return self.browser_agent.call(op)

        def browser_fill(self, target, text, submit=False):
            raw = str(target or "").strip()
            value = str(text or "")
            if bool(submit) and not getattr(self, "_action_broker_executing", False):
                return {"ok": False, "error": "confirmation_required", "detail": "Rellenar y enviar requiere autorización previa."}
            def op():
                page = self.browser_agent.page
                loc = self.browser_agent.resolve(raw)
                loc.fill(value)
                if bool(submit):
                    loc.press("Enter")
                return {"ok": True, "filled": True, "submitted": bool(submit), "url": page.url}
            return self.browser_agent.call(op)

        def browser_press(self, key):
            value = str(key or "Enter")
            def op():
                page = self.browser_agent.page
                page.keyboard.press(value)
                return {"ok": True, "key": value, "url": page.url}
            return self.browser_agent.call(op)

        def browser_tabs(self, activate=None):
            def op():
                context = self.browser_agent._context
                pages = list(context.pages)
                if activate is not None:
                    idx = int(activate)
                    if not (0 <= idx < len(pages)):
                        return {"ok": False, "error": "tab_out_of_range", "count": len(pages)}
                    self.browser_agent._page = pages[idx]
                    pages[idx].bring_to_front()
                active_page = self.browser_agent.page
                return {"ok": True, "active": pages.index(active_page) if active_page in pages else 0, "tabs": [{"index": i, "title": p.title(), "url": p.url} for i, p in enumerate(pages)]}
            return self.browser_agent.call(op)

        def browser_back(self):
            def op():
                page = self.browser_agent.page
                page.go_back(wait_until="domcontentloaded")
                return {"ok": True, "url": page.url, "title": page.title()}
            return self.browser_agent.call(op)

        def browser_reload(self):
            def op():
                page = self.browser_agent.page
                page.reload(wait_until="domcontentloaded")
                return {"ok": True, "url": page.url, "title": page.title()}
            return self.browser_agent.call(op)

        # ---------- Ventanas / UI Automation ----------
        @staticmethod
        def _desktop():
            from pywinauto import Desktop
            return Desktop(backend="uia")

        def window_list(self):
            try:
                rows = []
                for win in _desktop().windows():
                    try:
                        title = win.window_text().strip()
                        if title and win.is_visible():
                            rect = win.rectangle()
                            rows.append({"title": title[:300], "handle": int(win.handle), "rect": [rect.left, rect.top, rect.right, rect.bottom]})
                    except Exception:
                        continue
                return {"ok": True, "windows": rows[:120], "count": min(len(rows), 120)}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

        def _find_window(self, title=""):
            raw = str(title or "").strip()
            desktop = _desktop()
            if not raw:
                try:
                    return desktop.get_active()
                except Exception:
                    pass
            pattern = ".*" + re.escape(raw) + ".*"
            return desktop.window(title_re=pattern)

        def window_activate(self, title):
            try:
                win = _find_window(self, title)
                win.set_focus()
                return {"ok": True, "title": win.window_text(), "handle": int(win.handle)}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

        def window_close(self, title):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_window_close", False):
                return {"ok": False, "error": "confirmation_required", "detail": "Cerrar ventanas requiere confirmación según el perfil actual."}
            try:
                win = _find_window(self, title)
                label = win.window_text()
                win.close()
                return {"ok": True, "closed": label}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

        def window_move(self, title, x, y, width, height):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_window_layout", False):
                return {"ok": False, "error": "confirmation_required"}
            try:
                win = _find_window(self, title)
                win.move_window(int(x), int(y), max(100, int(width)), max(80, int(height)), repaint=True)
                return {"ok": True, "title": win.window_text(), "rect": [int(x), int(y), int(width), int(height)]}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

        def uia_snapshot(self, title="", limit=120):
            try:
                win = _find_window(self, title)
                controls = win.descendants()
                self._uia_refs = {}
                rows = []
                for ctrl in controls[: max(1, min(int(limit or 120), 300))]:
                    try:
                        info = ctrl.element_info
                        name = str(getattr(info, "name", "") or "").strip()
                        control_type = str(getattr(info, "control_type", "") or "")
                        auto_id = str(getattr(info, "automation_id", "") or "")
                        if not (name or auto_id):
                            continue
                        ref = len(rows) + 1
                        self._uia_refs[ref] = {"name": name, "auto_id": auto_id, "control_type": control_type, "window": str(title or "")}
                        rows.append({"ref": ref, "name": name[:240], "auto_id": auto_id[:160], "control_type": control_type})
                    except Exception:
                        continue
                return {"ok": True, "window": win.window_text(), "controls": rows, "count": len(rows)}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

        def _uia_control(self, window, control):
            win = _find_window(self, window)
            raw = str(control or "").strip()
            match = re.fullmatch(r"(?:ref\s*[=:]?\s*|#)?(\d+)", raw, flags=re.I)
            spec = self._uia_refs.get(int(match.group(1))) if match else None
            if spec:
                if spec.get("auto_id"):
                    return win.child_window(auto_id=spec["auto_id"]).wrapper_object()
                return win.child_window(title=spec.get("name"), control_type=spec.get("control_type") or None).wrapper_object()
            try:
                return win.child_window(auto_id=raw).wrapper_object()
            except Exception:
                return win.child_window(title_re=".*" + re.escape(raw) + ".*").wrapper_object()

        def uia_click(self, window="", control=""):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_uia_actions", False):
                return {"ok": False, "error": "confirmation_required"}
            try:
                ctrl = _uia_control(self, window, control)
                if hasattr(ctrl, "invoke"):
                    try:
                        ctrl.invoke()
                    except Exception:
                        ctrl.click_input()
                else:
                    ctrl.click_input()
                return {"ok": True, "control": str(control)}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

        def uia_type(self, window="", control="", text=""):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_uia_actions", False):
                return {"ok": False, "error": "confirmation_required"}
            try:
                ctrl = _uia_control(self, window, control)
                ctrl.set_focus()
                try:
                    ctrl.set_edit_text(str(text or ""))
                except Exception:
                    ctrl.type_keys(str(text or ""), with_spaces=True, set_foreground=True)
                return {"ok": True, "control": str(control), "chars": len(str(text or ""))}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

        # ---------- Entrada sintética (fallback) ----------
        def mouse_move(self, x, y):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_input_actions", False):
                return {"ok": False, "error": "confirmation_required"}
            try:
                from pynput.mouse import Controller
                Controller().position = (int(x), int(y))
                return {"ok": True, "x": int(x), "y": int(y)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:500]}

        def mouse_click(self, x, y, button="left", count=1):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_input_actions", False):
                return {"ok": False, "error": "confirmation_required"}
            try:
                from pynput.mouse import Button, Controller
                ctl = Controller()
                ctl.position = (int(x), int(y))
                btn = {"left": Button.left, "right": Button.right, "middle": Button.middle}.get(str(button).casefold(), Button.left)
                ctl.click(btn, max(1, min(int(count or 1), 3)))
                return {"ok": True, "x": int(x), "y": int(y), "button": str(button), "count": int(count or 1)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:500]}

        def keyboard_type(self, text):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_input_actions", False):
                return {"ok": False, "error": "confirmation_required"}
            try:
                from pynput.keyboard import Controller
                Controller().type(str(text or ""))
                return {"ok": True, "chars": len(str(text or ""))}
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:500]}

        def keyboard_press(self, key):
            if not getattr(self, "_action_broker_executing", False) and self.config.get("security", {}).get("confirm_input_actions", False):
                return {"ok": False, "error": "confirmation_required"}
            try:
                from pynput.keyboard import Controller, Key
                ctl = Controller()
                raw = str(key or "").casefold().strip()
                aliases = {"enter": Key.enter, "tab": Key.tab, "esc": Key.esc, "escape": Key.esc, "space": Key.space, "backspace": Key.backspace, "delete": Key.delete, "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right}
                parts = [x.strip() for x in raw.split("+") if x.strip()]
                mods = {"ctrl": Key.ctrl, "control": Key.ctrl, "alt": Key.alt, "shift": Key.shift, "win": Key.cmd, "cmd": Key.cmd}
                held = []
                for part in parts[:-1]:
                    k = mods.get(part)
                    if k:
                        ctl.press(k); held.append(k)
                last = parts[-1] if parts else raw
                target = aliases.get(last, last[0] if len(last) == 1 else last)
                ctl.press(target); ctl.release(target)
                for k in reversed(held):
                    ctl.release(k)
                return {"ok": True, "key": raw}
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:500]}

        LocalTools.__init__ = init
        for name, fn in {
            "browser_open": browser_open, "browser_search": browser_search, "browser_read": browser_read,
            "browser_inspect": browser_inspect, "browser_click": browser_click, "browser_fill": browser_fill,
            "browser_press": browser_press, "browser_tabs": browser_tabs, "browser_back": browser_back,
            "browser_reload": browser_reload, "window_list": window_list, "window_activate": window_activate,
            "window_close": window_close, "window_move": window_move, "uia_snapshot": uia_snapshot,
            "uia_click": uia_click, "uia_type": uia_type, "mouse_move": mouse_move, "mouse_click": mouse_click,
            "keyboard_type": keyboard_type, "keyboard_press": keyboard_press,
        }.items():
            setattr(LocalTools, name, fn)
        LocalTools._nova_desktop_core = True

    original_selector = mod.select_tool_schemas
    if not getattr(original_selector, "_nova_desktop_core", False):
        by_name = {x["function"]["name"]: x for x in mod.TOOL_SCHEMAS}
        browser_names = {x["function"]["name"] for x in BROWSER_SCHEMAS}
        desktop_names = {x["function"]["name"] for x in DESKTOP_SCHEMAS}
        browser_cues = ("navegador", "browser", "página", "pagina", "web", "sitio", "google", "busca en internet", "click", "formulario", "pestaña")
        desktop_cues = ("ventana", "aplicación", "aplicacion", "uia", "control", "botón", "boton", "mouse", "ratón", "raton", "teclado", "escribe en")

        def selector(text):
            rows = list(original_selector(text))
            present = {x.get("function", {}).get("name") for x in rows}
            raw = str(text or "").casefold()
            wanted = set()
            if any(cue in raw for cue in browser_cues):
                wanted |= browser_names
            if any(cue in raw for cue in desktop_cues):
                wanted |= desktop_names
            rows += [by_name[name] for name in wanted if name in by_name and name not in present]
            return rows

        selector._nova_desktop_core = True
        mod.select_tool_schemas = selector

    return mod
