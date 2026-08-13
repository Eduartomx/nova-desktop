from __future__ import annotations

"""Sincronización dirigida por eventos entre Gaming Awareness y Tkinter."""

import queue


def gaming_label(report):
    if not report.get("enabled", True):
        return "🎮 Juego: desactivado"
    mode = str(report.get("mode") or "auto")
    if mode == "off":
        return "🎮 Juego: manual off"
    if report.get("active"):
        game = report.get("game") or {}
        name = str(game.get("process") or "Gaming Mode")
        source = str(game.get("source") or "?")
        prefix = f"🎮 {name} · {source}"
        if report.get("llm_released"):
            return prefix + " · VRAM priorizada"
        if report.get("keep_llm_loaded_during_game"):
            return prefix + " · Qwen mantenido"
        return prefix + " · Gaming Mode"
    return "🎮 Juego: auto" if mode == "auto" else "🎮 Juego: preparado"


def apply_gaming_report(ui, report):
    try:
        ui.gaming_mode_var.set(gaming_label(report))
    except Exception:
        pass
    if not hasattr(ui, "llm_warm_var"):
        return
    try:
        if report.get("active") and report.get("llm_released"):
            ui.llm_warm_var.set("LLM: liberado · Gaming Mode")
            return
        warm = getattr(ui, "llm_warm_manager", None)
        if warm is None:
            return
        from .ui_gaming import _warm_label
        ui.llm_warm_var.set(_warm_label(warm.cached_status()))
    except Exception:
        pass


def install_ui_gaming_events():
    from . import ui as mod
    from . import ui_gaming as gaming_ui

    UI = mod.AssistantUI
    if getattr(UI, "_nova_gaming_events_patched", False):
        return mod

    gaming_ui._gaming_label = gaming_label
    original_init = UI.__init__
    original_close = UI._close

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._gaming_event_queue = queue.SimpleQueue()
        manager = getattr(self, "gaming_awareness", None)

        def listener(event, report):
            # El hilo de Gaming Awareness solo encola; nunca toca Tk directamente.
            try:
                self._gaming_event_queue.put((str(event), dict(report)))
            except Exception:
                pass

        self._gaming_state_listener = listener
        if manager is not None and hasattr(manager, "add_state_listener"):
            try:
                manager.add_state_listener(listener)
            except Exception:
                pass
        try:
            self.root.after(60, self._gaming_event_drain)
        except Exception:
            pass

    def _gaming_event_drain(self):
        if getattr(self, "_closing", False):
            return
        latest = None
        q = getattr(self, "_gaming_event_queue", None)
        if q is not None:
            while True:
                try:
                    latest = q.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
        if latest is not None:
            _event, report = latest
            apply_gaming_report(self, report)
        try:
            self.root.after(90, self._gaming_event_drain)
        except Exception:
            pass

    def _close(self):
        manager = getattr(self, "gaming_awareness", None)
        listener = getattr(self, "_gaming_state_listener", None)
        if manager is not None and listener is not None and hasattr(manager, "remove_state_listener"):
            try:
                manager.remove_state_listener(listener)
            except Exception:
                pass
        return original_close(self)

    UI.__init__ = init
    UI._gaming_event_drain = _gaming_event_drain
    UI._close = _close
    UI._nova_gaming_events_patched = True
    return mod
