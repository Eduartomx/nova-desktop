from __future__ import annotations

from .event_vision import get_event_vision


def install_ui_vision():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_event_vision_patched", False):
        return mod

    original_init = UI.__init__
    original_close = getattr(UI, "_close", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            vision = get_event_vision(self.config, self.agent.memory)
            vision.start()
            self.event_vision = vision
        except Exception:
            self.event_vision = None

    def close(self, *args, **kwargs):
        try:
            vision = getattr(self, "event_vision", None)
            if vision is not None:
                vision.stop()
        except Exception:
            pass
        if callable(original_close):
            return original_close(self, *args, **kwargs)
        try:
            return self.root.destroy()
        except Exception:
            return None

    UI.__init__ = init
    if callable(original_close):
        UI._close = close
    UI._nova_event_vision_patched = True
    return mod
