from __future__ import annotations


def install_ui_v065():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_v065_patched", False):
        return mod

    original_init = UI.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            self.root.after(1300, self._continuity_startup_hint)
        except Exception:
            pass

    def continuity_startup_hint(self):
        try:
            active = self.agent.memory.active_workspace()
            if not active:
                return
            state = self.agent.memory.continuity_pending(workspace_id=int(active["id"]), any_if_none=False)
            items = list(state.get("pending_items") or [])
            session = state.get("session") or {}
            if not state.get("ok") or not items:
                return
            status = str(session.get("status") or "active")
            first = str(items[0])
            extra = max(0, len(items) - 1)
            text = f"Continuity · {active.get('name')} · sesión {status} · pendiente: {first}"
            if extra:
                text += f" (+{extra})"
            text += ". Puedes decir «Nova, continúa»."
            self._append("system", text)
        except Exception:
            pass

    UI.__init__ = init
    UI._continuity_startup_hint = continuity_startup_hint
    UI._nova_v065_patched = True
    return mod
