from __future__ import annotations

from .perception import get_perception


def install_ui_perception():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_perception_patched", False):
        return mod

    original_init = UI.__init__
    original_close = getattr(UI, "_close", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            engine = get_perception(self.config, self.agent.memory)
            engine.attach_memory(self.agent.memory).start()
            self.perception = engine
            self.root.after(700, self._perception_startup_hint)
        except Exception:
            self.perception = None

    def startup_hint(self):
        try:
            engine = getattr(self, "perception", None)
            if engine is None or not engine.enabled:
                return
            self._append(
                "system",
                "Perception Engine activo · contexto de aplicación/ventana y sistema por metadatos locales; sin captura continua de pantalla, teclado ni portapapeles.",
            )
        except Exception:
            pass

    def close(self, *args, **kwargs):
        try:
            engine = getattr(self, "perception", None)
            if engine is not None:
                engine.stop(timeout=0.25)
        except Exception:
            pass
        if callable(original_close):
            return original_close(self, *args, **kwargs)
        try:
            return self.root.destroy()
        except Exception:
            return None

    UI.__init__ = init
    UI._perception_startup_hint = startup_hint
    if callable(original_close):
        UI._close = close
    UI._nova_perception_patched = True
    return mod
