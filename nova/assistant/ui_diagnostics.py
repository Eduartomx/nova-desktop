from __future__ import annotations

import threading

from .doctor import NovaDoctor
from .llm_performance import get_llm_performance
from .profiler import get_profiler
from .self_repair import SelfRepairManager


def _format_llm_windows(windows):
    labels = (("session", "Sesión actual"), ("15m", "Últimos 15 min"), ("1h", "Última hora"), ("24h", "Últimas 24 h"))
    lines = ["LLM por ventana"]
    for key, label in labels:
        report = (windows or {}).get(key) or {}
        if not report.get("calls"):
            lines.append(f"- {label}: sin llamadas")
            continue
        line = (
            f"- {label}: {report.get('calls')} llamadas · {report.get('avg_wall_ms')} ms prom. · "
            f"{report.get('avg_eval_tps')} tok/s"
        )
        if report.get("failures"):
            line += f" · {report.get('failures')} fallos"
        lines.append(line)
    return "\n".join(lines)


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
        profiler = get_profiler(self.config)
        perf_windows = report.get("performance_windows") if isinstance(report.get("performance_windows"), dict) else {}
        if perf_windows:
            text += "\n\n" + profiler.format_windows(perf_windows)
        else:
            perf = report.get("performance") if isinstance(report.get("performance"), dict) else {}
            if perf:
                text += "\n\n" + profiler.format_summary(perf)

        llm_monitor = get_llm_performance(self.config)
        llm_windows = report.get("llm_performance_windows") if isinstance(report.get("llm_performance_windows"), dict) else {}
        if llm_windows:
            text += "\n\n" + _format_llm_windows(llm_windows)
            session = llm_windows.get("session") or {}
            text += "\n\n" + llm_monitor.format_summary(session, title="Detalle LLM · sesión actual")

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
        win.geometry("860x700")
        win.minsize(680, 500)

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
