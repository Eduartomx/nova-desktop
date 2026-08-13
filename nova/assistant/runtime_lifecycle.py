from __future__ import annotations

"""Native resident lifecycle for Nova.

Window hiding and process shutdown are separate operations. The manager is
idempotent and runs real shutdown on Tk's scheduler without using sleeps.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Callable

LIFECYCLE_STATES = {"starting", "running", "hidden", "shutting_down", "stopped"}
_CURRENT_LIFECYCLE = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")[:360]


@dataclass(frozen=True)
class LifecycleError:
    component: str
    detail: str
    timestamp: str


class RuntimeLifecycleManager:
    def __init__(self, root, config=None, *, ui=None, scheduler=None, data_root=None):
        self.root = root
        self.config = config or {}
        self.ui = ui
        self._scheduler: Callable[[Callable[[], Any]], Any] = scheduler or (lambda fn: root.after(0, fn))
        self._lock = threading.RLock()
        self._state = "starting"
        self._shutdown_scheduled = False
        self._shutdown_performed = False
        self._accepting_commands = True
        self._last_shutdown_reason = ""
        self._errors: list[LifecycleError] = []
        self.tray = None
        self.instance = None
        self.autostart = None
        self.session_hook = None
        base = Path(data_root) if data_root is not None else Path(__file__).resolve().parent.parent / "data"
        self._status_path = base / "runtime_lifecycle.json"
        self._previous = self._load_previous_status()
        global _CURRENT_LIFECYCLE
        _CURRENT_LIFECYCLE = self

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def accepting_commands(self):
        with self._lock:
            return self._accepting_commands

    @property
    def window_hidden(self):
        return self.state == "hidden"

    def attach_ui(self, ui):
        self.ui = ui
        return self

    def attach_tray(self, tray):
        self.tray = tray
        return self

    def attach_instance(self, instance):
        self.instance = instance
        return self

    def attach_autostart(self, autostart):
        self.autostart = autostart
        return self

    def attach_session_hook(self, hook):
        self.session_hook = hook
        return self

    def _set_state(self, state):
        if state not in LIFECYCLE_STATES:
            raise ValueError(f"invalid lifecycle state: {state}")
        with self._lock:
            self._state = state
        self._persist_status()

    def _can_hide_to_tray(self):
        cfg = self.config.get("resident_mode", {}) if isinstance(self.config, dict) else {}
        return bool(cfg.get("enabled", True) and cfg.get("close_to_tray", True) and self.tray is not None and getattr(self.tray, "available", False))

    def mark_running(self, *, start_hidden=False):
        if start_hidden and self._can_hide_to_tray():
            self._hide_now()
        else:
            self._set_state("running")

    def request_window_close(self):
        self._scheduler(self._window_close_on_tk)
        return True

    def _window_close_on_tk(self):
        if self.state in {"shutting_down", "stopped"}:
            return
        if self._can_hide_to_tray():
            self._hide_now()
        else:
            self.request_shutdown("window_close")

    def hide_window(self):
        if not self._can_hide_to_tray():
            return False
        self._scheduler(self._hide_now)
        return True

    def _hide_now(self):
        if self.state in {"shutting_down", "stopped"} or not self._can_hide_to_tray():
            return
        try:
            self.root.withdraw()
            self._set_state("hidden")
        except Exception as exc:
            self._record_error("window_hide", exc)

    def show_window(self):
        if self.state in {"shutting_down", "stopped"}:
            return False
        self._scheduler(self._show_now)
        return True

    def _show_now(self):
        if self.state in {"shutting_down", "stopped"}:
            return
        try:
            self.root.deiconify()
            self.root.lift()
            try:
                self.root.attributes("-topmost", True)
                self.root.after(120, lambda: self.root.attributes("-topmost", False))
            except Exception:
                pass
            entry = getattr(self.ui, "input_entry", None)
            if entry is not None:
                try:
                    entry.focus_set()
                except Exception:
                    pass
            self._set_state("running")
        except Exception as exc:
            self._record_error("window_show", exc)

    def request_shutdown(self, reason="user_exit"):
        reason = str(reason or "user_exit")[:80]
        with self._lock:
            if self._shutdown_scheduled or self._shutdown_performed or self._state in {"shutting_down", "stopped"}:
                return False
            self._shutdown_scheduled = True
            self._accepting_commands = False
            self._last_shutdown_reason = reason
            self._state = "shutting_down"
        self._persist_status()
        self._scheduler(self._perform_shutdown)
        return True

    def _run_step(self, component, callback):
        if not callable(callback):
            return
        try:
            callback()
        except Exception as exc:
            self._record_error(component, exc)

    def _perform_shutdown(self):
        with self._lock:
            if self._shutdown_performed:
                return
            self._shutdown_performed = True
            self._shutdown_scheduled = True
            self._accepting_commands = False
            self._state = "shutting_down"
        ui = self.ui

        def block_new_work():
            if ui is None:
                return
            ui._closing = True
            ui._accepting_commands = False
            for listener in (getattr(ui, "_hotkey_listener", None), getattr(ui, "_ptt_listener", None)):
                if listener is not None:
                    listener.stop()
        self._run_step("block_new_work", block_new_work)

        def stop_voice():
            if ui is None:
                return
            if getattr(ui, "_recording", False) and hasattr(ui, "_stop_recording"):
                ui._stop_recording()
            stop_event = getattr(ui, "_wake_stop", None)
            if stop_event is not None:
                stop_event.set()
            thread = getattr(ui, "_wake_thread", None)
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=0.8)
        self._run_step("voice_wake", stop_voice)

        def stop_local_services():
            if ui is None:
                return
            seen = set()
            for name in ("gaming_awareness", "perception", "anomaly_detector", "anomaly_detection", "event_vision", "vision_engine", "workspace_autodetect", "workspace_autodetector"):
                service = getattr(ui, name, None)
                if service is None or id(service) in seen:
                    continue
                seen.add(id(service))
                stop = getattr(service, "stop", None)
                if callable(stop):
                    try:
                        stop(timeout=0.8)
                    except TypeError:
                        stop()
            manager = getattr(ui, "gaming_awareness", None)
            listener = getattr(ui, "_gaming_state_listener", None)
            if manager is not None and listener is not None and hasattr(manager, "remove_state_listener"):
                manager.remove_state_listener(listener)
        self._run_step("local_services", stop_local_services)

        def stop_browser():
            from .tools_desktop import _stop_all
            _stop_all()
        self._run_step("browser_agent", stop_browser)

        def save_pending():
            if ui is None:
                return
            agent = getattr(ui, "agent", None)
            for obj in (agent, getattr(agent, "memory", None), getattr(agent, "continuity", None)):
                flush = getattr(obj, "flush", None)
                if callable(flush):
                    flush()
        self._run_step("pending_state", save_pending)

        def final_qwen_policy():
            if ui is None:
                return
            warm = getattr(ui, "llm_warm_manager", None) or getattr(getattr(ui, "agent", None), "llm_warm", None)
            if warm is not None and bool(getattr(warm, "unload_on_exit", True)):
                warm.unload(timeout=1.2, reason=self._last_shutdown_reason or "runtime_exit")
        self._run_step("llm_warm", final_qwen_policy)
        self._run_step("tray", getattr(self.tray, "stop", None))
        self._run_step("windows_session_hook", getattr(self.session_hook, "uninstall", None))
        self._run_step("single_instance", getattr(self.instance, "release", None))
        self._run_step("tk_destroy", getattr(self.root, "destroy", None))
        with self._lock:
            self._state = "stopped"
        self._persist_status()

    def handle_control_command(self, command):
        command = str(command or "").strip().casefold()
        if command == "show":
            return {"ok": self.show_window(), "command": command}
        if command == "shutdown_for_update":
            accepted = self.request_shutdown("update")
            return {"ok": accepted or self.state in {"shutting_down", "stopped"}, "command": command}
        if command == "status":
            return {"ok": True, "command": command, "status": self.status()}
        return {"ok": False, "error": "unsupported_command"}

    def notify_task_completed(self):
        if self.state == "hidden" and self.tray is not None:
            notify = getattr(self.tray, "notify", None)
            if callable(notify):
                notify("task_completed", "Tarea completada", "Nova terminó una tarea mientras estaba en segundo plano.")

    def status(self):
        cfg = self.config.get("resident_mode", {}) if isinstance(self.config, dict) else {}
        tray_status = getattr(self.tray, "status", lambda: {"available": False, "degraded": True})()
        instance_status = getattr(self.instance, "status", lambda: {"acquired": False})()
        autostart_status = getattr(self.autostart, "status", lambda: {"enabled": False, "present": False})()
        return {
            "state": self.state,
            "resident_enabled": bool(cfg.get("enabled", True)),
            "close_to_tray": bool(cfg.get("close_to_tray", True)),
            "window_hidden": self.window_hidden,
            "tray": tray_status,
            "single_instance": instance_status,
            "start_with_windows": autostart_status,
            "last_shutdown_reason": self._last_shutdown_reason or str(self._previous.get("last_shutdown_reason") or ""),
            "recent_errors": [e.__dict__ for e in self._errors[-6:]],
            "accepting_commands": self.accepting_commands,
        }

    def _load_previous_status(self):
        try:
            if self._status_path.exists():
                data = json.loads(self._status_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def _persist_status(self):
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"state": self.state, "last_shutdown_reason": self._last_shutdown_reason, "updated_at": _now(), "recent_errors": [e.__dict__ for e in self._errors[-6:]]}
            self._status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return

    def _record_error(self, component, exc):
        with self._lock:
            self._errors.append(LifecycleError(str(component)[:80], _safe_error(exc), _now()))
            self._errors = self._errors[-12:]
        self._persist_status()


def get_current_lifecycle():
    return _CURRENT_LIFECYCLE
