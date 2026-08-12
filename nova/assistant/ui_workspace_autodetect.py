from __future__ import annotations

from .workspace_autodetect import get_workspace_autodetector


def install_ui_workspace_autodetect():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_workspace_autodetect_patched", False):
        return mod

    original_init = UI.__init__
    original_close = getattr(UI, "_close", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            detector = get_workspace_autodetector(self.config, self.agent.memory)
            detector.attach_memory(self.agent.memory).start()
            self.workspace_autodetect = detector
        except Exception:
            self.workspace_autodetect = None

    def close(self, *args, **kwargs):
        try:
            detector = getattr(self, "workspace_autodetect", None)
            if detector is not None:
                detector.stop(timeout=0.25)
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
    UI._nova_workspace_autodetect_patched = True
    return mod
