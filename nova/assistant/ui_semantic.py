from __future__ import annotations

from pathlib import Path


def install_ui_v063():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_v063_patched", False):
        return mod

    original_init = UI.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            root = Path(__file__).resolve().parent.parent
            version = (root / "NOVA_VERSION.txt").read_text(encoding="utf-8", errors="ignore").strip()
            self.root.title(f"{self.name} · Asistente local v{version}")
        except Exception:
            pass
        try:
            # Startup no debe bloquear esperando Ollama/red. La comprobación activa
            # sigue disponible por comando y en Nova Doctor.
            status = self.agent.memory.semantic_status(refresh=False)
            if status.get("enabled") and status.get("model_available"):
                self._append("system", f"Semantic Memory activa · {status.get('model')} · búsqueda híbrida local disponible.")
            elif status.get("enabled"):
                self._append("system", f"Semantic Memory preparada · estado del modelo pendiente de comprobación. Nova mantiene búsqueda léxica como fallback.")
        except Exception:
            pass

    UI.__init__ = init
    UI._nova_v063_patched = True
    return mod
