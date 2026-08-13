from __future__ import annotations


def install_ui_reliability():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_reliability_patched", False):
        return mod

    original_refresh = getattr(UI, "_refresh_skills_manager", None)
    original_show = getattr(UI, "_skills_show_selected", None)

    if callable(original_refresh):
        def refresh(self):
            original_refresh(self)
            engine = getattr(self.agent, "skill_reliability", None)
            lb = getattr(self, "skills_listbox", None)
            rows = list(getattr(self, "skills_rows", []) or [])
            if engine is None or lb is None:
                return
            try:
                lb.delete(0, "end")
                for row in rows:
                    rel = engine.report(row)
                    state = "✓" if row.get("enabled") else "×"
                    scope = "workspace" if row.get("workspace_id") is not None else "global"
                    band = str(rel.get("band") or "unproven")
                    marker = "⚠" if rel.get("needs_review") else "·"
                    lb.insert(
                        "end",
                        f"{state} {marker} {row.get('name')}  [v{row.get('version')} · {row.get('trust_level')} · {band} · {scope}]",
                    )
            except Exception:
                pass

        UI._refresh_skills_manager = refresh

    if callable(original_show):
        def show_selected(self):
            original_show(self)
            engine = getattr(self.agent, "skill_reliability", None)
            row = self._selected_skill() if hasattr(self, "_selected_skill") else None
            widget = getattr(self, "skills_detail", None)
            if engine is None or not row or widget is None:
                return
            try:
                rel = engine.report(row)
                widget.configure(state="normal")
                widget.insert(
                    "end",
                    "\n\nFiabilidad reciente:\n"
                    f"- Estado: {rel.get('band')}\n"
                    f"- Índice histórico: {float(rel.get('score',0.5)):.2f}/1.00\n"
                    f"- Ventana: {rel.get('successes',0)} correctas · {rel.get('failures',0)} fallidas\n"
                    f"- Fallos consecutivos: {rel.get('consecutive_failures',0)}\n"
                    f"- Motivo: {rel.get('reason') or '-'}\n"
                    f"- Requiere revisión: {'sí' if rel.get('needs_review') else 'no'}"
                )
                if rel.get("needs_review"):
                    widget.insert("end", "\n\n⚠ Nova recomienda revisar esta Skill antes de reutilizarla.")
                widget.configure(state="disabled")
            except Exception:
                try:
                    widget.configure(state="disabled")
                except Exception:
                    pass

        UI._skills_show_selected = show_selected

    UI._nova_reliability_patched = True
    return mod
