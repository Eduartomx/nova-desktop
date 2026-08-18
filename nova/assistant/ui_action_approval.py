from __future__ import annotations

"""Tk-only approval surface for Action Broker requests.

No model, webpage, clipboard, voice command, IPC message or text file can call
the approval methods exposed here.  The handler is installed directly on the
in-process broker owned by the UI's LocalTools instance.
"""

import tkinter as tk


class ActionApprovalController:
    def __init__(self, ui):
        self.ui = ui
        self.root = ui.root
        self.broker = ui.agent.tools.action_broker
        self._windows: dict[str, tk.Toplevel] = {}
        self.broker.set_approval_handler(self.enqueue)

    def enqueue(self, request: dict) -> None:
        self.root.after(0, lambda row=dict(request): self._show(row))

    def _show(self, request: dict) -> None:
        request_id = str(request.get("request_id") or "")
        if not request_id or request_id in self._windows:
            return
        try:
            if getattr(self.ui.runtime_lifecycle, "window_hidden", False):
                tray = getattr(self.ui, "tray_controller", None)
                if tray is not None:
                    tray.notify("action_approval", "Nova espera tu permiso", "Abre Nova para aprobar o denegar una acción.")

            win = tk.Toplevel(self.root)
            self._windows[request_id] = win
            win.title("Nova · Autorizar acción")
            win.transient(self.root)
            win.resizable(False, False)
            frame = tk.Frame(win, padx=16, pady=14)
            frame.pack(fill="both", expand=True)
            tk.Label(frame, text="Nova necesita tu autorización", font=("Segoe UI", 12, "bold")).pack(anchor="w")
            tk.Label(frame, text=f"Acción: {request.get('tool', '')}", anchor="w").pack(fill="x", pady=(10, 0))
            tk.Label(frame, text=f"Riesgo: {request.get('risk', '')} · Efecto: {request.get('effect', '')}", anchor="w").pack(fill="x")
            target = str(request.get("target") or "")
            if target:
                tk.Label(frame, text=f"Objetivo: {target}", anchor="w", wraplength=520, justify="left").pack(fill="x")
            tk.Label(frame, text=str(request.get("reason") or ""), anchor="w", wraplength=520, justify="left", fg="#555").pack(fill="x", pady=(4, 10))

            buttons = tk.Frame(frame)
            buttons.pack(fill="x")
            tk.Button(buttons, text="Permitir una vez", command=lambda: self._decide(request_id, "once"), width=17).pack(side="left")
            if request.get("allow_task_grant") and request.get("task_id"):
                tk.Button(buttons, text="Durante esta tarea", command=lambda: self._decide(request_id, "task"), width=18).pack(side="left", padx=8)
            tk.Button(buttons, text="Denegar", command=lambda: self._decide(request_id, "deny"), width=12).pack(side="right")
            win.protocol("WM_DELETE_WINDOW", lambda: self._decide(request_id, "deny"))
            win.lift()
            win.attributes("-topmost", True)
            win.after(150, lambda: win.attributes("-topmost", False))
        except Exception:
            self.broker.deny(request_id, reason="ui_unavailable")

    def _decide(self, request_id: str, mode: str) -> None:
        if mode == "deny":
            self.broker.deny(request_id)
        else:
            self.broker.approve(request_id, mode=mode)
        win = self._windows.pop(request_id, None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def cancel_all(self, reason="cancelled", *, shutdown=False) -> int:
        count = self.broker.cancel_all(reason, shutdown=shutdown)
        for request_id, win in list(self._windows.items()):
            try:
                win.destroy()
            except Exception:
                pass
            self._windows.pop(request_id, None)
        return count


def attach_action_approval(ui):
    tools = getattr(getattr(ui, "agent", None), "tools", None)
    if tools is None or not hasattr(tools, "action_broker"):
        return None
    controller = ActionApprovalController(ui)
    ui.action_approval = controller
    return controller
