from __future__ import annotations

from .anomaly_detection import get_anomaly_detector


def install_ui_anomaly():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_anomaly_patched", False):
        return mod

    original_init = UI.__init__
    original_close = getattr(UI, "_close", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            detector = get_anomaly_detector(self.config, self.agent.memory)
            detector.attach_memory(self.agent.memory).start()
            self.anomaly_detector = detector
        except Exception:
            self.anomaly_detector = None

    def close(self, *args, **kwargs):
        try:
            detector = getattr(self, "anomaly_detector", None)
            if detector is not None:
                detector.stop(timeout=0.35)
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
    UI._nova_anomaly_patched = True
    return mod
