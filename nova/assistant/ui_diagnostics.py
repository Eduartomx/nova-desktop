from __future__ import annotations

import threading

from .doctor import NovaDoctor
from .profiler import get_profiler
from .self_repair import SelfRepairManager


def install_ui_v066():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_v066_patched", False):
        return mod

    tk = mod.tk
    messagebox = mod.messagebox
    original_init = UI.__init__

    def init(self, *args, **kwargs):
        self.doctor_window = None
        self.doctor_text = None
        self.doctor_actions_frame = None
        self.doctor_status_var = None
        original_init(self, *args, **kwargs)
        try:
            hotkey = str(self.config.get("hotkey", "<ctrl>+<alt>+<space>"))
            self._append("system", f"Atajo global de Nova: {hotkey} · configurable en config.json y aplicado al reiniciar.")
        except Exception:
            pass

    def _doctor_render(self, report):
        if self.doctor_window is None:
            return
        try:
            if not self.doctor_window.winfo_exists():
                return
        except Exception:
            return

        text = NovaDoctor.format_text(report)
        perf = report.get("performance") if isinstance(report.get("performance"), dict) else {}
        if perf:
            text += "\n\n" + get_profiler(self.config).format_summary(perf)
        widget = self.doctor_text
        if widget is not None:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            widget.configure(state="disabled")

        frame = self.doctor_actions_frame
        if frame is not None:
            for child in frame.winfo_children():
                child.destroy()
            repairs = list(report.get("repairs") or [])
            if repairs:
                tk.Label(frame, text="Reparaciones disponibles", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
                for action in repairs[:8]:
                    title = str(action.get("title") or action.get("id"))
                    tk.Button(
                        frame,
                        text="🛠 " + title,
                        anchor="w",
                        command=lambda a=dict(action): self._doctor_confirm_repair(a),
                    ).pack(fill="x", pady=2)
            else:
                tk.Label(frame, text="No hay reparaciones deterministas pendientes.").pack(anchor="w")
        if self.doctor_status_var is not None:
            self.doctor_status_var.set(f"Diagnóstico completado en {report.get('duration_seconds', '?')} s")

    def _doctor_run(self):
        if self.doctor_status_var is not None:
            self.doctor_status_var.set("Comprobando componentes…")

        def worker():
            try:
                report = NovaDoctor(self.config, self.agent.memory).run()
                self.root.after(0, lambda r=report: self._doctor_render(r))
            except Exception as exc:
                error_text = str(exc)
                self.root.after(
                    0,
                    lambda err=error_text: self.doctor_status_var.set(f"Error de Nova Doctor: {err}")
                    if self.doctor_status_var is not None else None,
                )

        threading.Thread(target=worker, daemon=True, name="nova-doctor-ui").start()

    def _doctor_confirm_repair(self, action):
        title = str(action.get("title") or action.get("id") or "Reparación")
        detail = str(action.get("detail") or "")
        risk = str(action.get("risk") or "medium")
        extra = ""
        if risk == "high":
            extra = "\n\nEsta reparación puede instalar o descargar software/modelos y puede tardar varios minutos."
        if not messagebox.askyesno(
            "Nova Doctor · Confirmar reparación",
            f"{title}\n\n{detail}{extra}\n\n¿Ejecutar esta reparación?",
            parent=self.doctor_window or self.root,
        ):
            return

        if self.doctor_status_var is not None:
            self.doctor_status_var.set(f"Ejecutando: {title}…")

        def worker():
            manager = SelfRepairManager(self.config, self.agent.memory)
            result = manager.execute(str(action.get("id") or ""))

            def done():
                if result.get("ok"):
                    messagebox.showinfo("Nova Doctor", f"{title}\n\nReparación completada.", parent=self.doctor_window or self.root)
                else:
                    detail_error = str(result.get("error") or result.get("stderr") or result.get("stdout") or "Error desconocido")
                    messagebox.showerror("Nova Doctor", f"{title}\n\nNo se pudo completar:\n{detail_error[-1800:]}", parent=self.doctor_window or self.root)
                self._doctor_run()

            self.root.after(0, done)

        threading.Thread(target=worker, daemon=True, name="nova-self-repair").start()

    def quick_doctor(self):
        if self.doctor_window is not None:
            try:
                if self.doctor_window.winfo_exists():
                    self.doctor_window.deiconify()
                    self.doctor_window.lift()
                    self._doctor_run()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self.doctor_window = win
        win.title("Nova Doctor · Reparación y rendimiento")
        win.geometry("820x650")
        win.minsize(650, 480)

        head = tk.Frame(win, padx=12, pady=10)
        head.pack(fill="x")
        tk.Label(head, text="Nova Doctor", font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Button(head, text="↻ Volver a comprobar", command=self._doctor_run).pack(side="right")

        status = tk.StringVar(value="Preparando diagnóstico…")
        self.doctor_status_var = status
        tk.Label(win, textvariable=status, anchor="w", padx=12).pack(fill="x")

        body = tk.Frame(win, padx=12, pady=8)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", font=("Consolas", 9), height=20)
        text.pack(fill="both", expand=True)
        text.configure(state="disabled")
        self.doctor_text = text

        actions = tk.Frame(win, padx=12, pady=10)
        actions.pack(fill="x")
        self.doctor_actions_frame = actions

        win.protocol("WM_DELETE_WINDOW", lambda: win.withdraw())
        self._doctor_run()

    UI.__init__ = init
    UI.quick_doctor = quick_doctor
    UI._doctor_run = _doctor_run
    UI._doctor_render = _doctor_render
    UI._doctor_confirm_repair = _doctor_confirm_repair
    UI._nova_v066_patched = True
    return mod
