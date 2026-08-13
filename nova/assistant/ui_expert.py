from __future__ import annotations

import threading


def install_ui_expert():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_expert_patched", False):
        return mod

    tk = mod.tk
    messagebox = mod.messagebox
    original_init = UI.__init__
    original_build = UI._build

    def init(self, *args, **kwargs):
        self.expert_window = None
        self.expert_status_text = None
        self.expert_free_button = None
        original_init(self, *args, **kwargs)

    def build(self):
        original_build(self)
        placed = False
        try:
            for frame in self.root.winfo_children():
                if not isinstance(frame, tk.Frame):
                    continue
                buttons = [x for x in frame.winfo_children() if isinstance(x, tk.Button)]
                labels = {str(button.cget("text")) for button in buttons}
                if "🧩 Skills" in labels or "📁 Proyectos" in labels:
                    tk.Button(frame, text="🧠 Experto", command=self.show_expert_manager, width=11).pack(
                        side="right", padx=(0, 6)
                    )
                    placed = True
                    break
        except Exception:
            placed = False
        if not placed:
            bar = tk.Frame(self.root, padx=12, pady=2)
            bar.pack(fill="x", before=self.chat)
            tk.Button(bar, text="🧠 Experto", command=self.show_expert_manager, width=11).pack(side="right")

    def format_status(self):
        service = getattr(self.agent, "expert", None)
        if service is None:
            return "Expert Escalation no está disponible."
        status = service.status()
        providers = status.get("providers") or {}
        lines = [
            "Expert Escalation",
            "",
            f"Estado: {'activo' if status.get('enabled') else 'desactivado'}",
            f"Segunda opinión gratuita automática: {'sí' if status.get('auto_free_second_opinion') else 'no'}",
            f"Candidata actual en memoria: {'sí' if status.get('candidate_in_memory') else 'no'}",
            "",
            "Proveedores gratuitos:",
        ]
        for name in status.get("provider_order") or []:
            row = providers.get(name) or {}
            lines.append(
                f"- {name}: {row.get('model') or '?'} · "
                f"{'LISTO' if row.get('key_present') else 'falta ' + str(row.get('api_key_env') or 'API key')}"
            )
        lines += [
            "",
            f"ChatGPT Assisted: {'disponible' if status.get('chatgpt_assisted') else 'desactivado'}",
            "Nova abre ChatGPT y copia la consulta, pero no pulsa Enviar ni extrae la respuesta automáticamente.",
            "",
            f"Eventos: {status.get('events', 0)} · opiniones gratuitas correctas: {status.get('successful_free_opinions', 0)}",
            "",
            "Privacidad: prompts y respuestas externas no se persisten en expert_escalation.db.",
            "Las API keys se leen exclusivamente desde variables de entorno.",
        ]
        return "\n".join(lines)

    def refresh(self):
        widget = self.expert_status_text
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", self._expert_format_status())
        widget.configure(state="disabled")

    def prepare_chatgpt(self):
        service = getattr(self.agent, "expert", None)
        if service is None:
            return
        if not service.last_candidate():
            messagebox.showinfo(
                "Nova · Experto",
                "Todavía no hay una petición candidata en memoria. Puedes decirle a Nova: «pregunta a ChatGPT sobre ...».",
                parent=self.expert_window or self.root,
            )
            return
        result = service.prepare_chatgpt(trigger="ui_explicit")
        if result.get("ok"):
            messagebox.showinfo(
                "Nova · ChatGPT Assisted",
                "Consulta preparada. Pégala/envíala en ChatGPT manualmente. Luego copia la respuesta y dile a Nova: «importa la respuesta de ChatGPT».",
                parent=self.expert_window or self.root,
            )
        else:
            messagebox.showerror(
                "Nova · ChatGPT Assisted",
                str(result.get("copy_error") or result.get("open_error") or "No pude preparar la consulta."),
                parent=self.expert_window or self.root,
            )
        self._refresh_expert_manager()

    def free_opinion(self):
        service = getattr(self.agent, "expert", None)
        if service is None:
            return
        if not service.last_candidate():
            messagebox.showinfo(
                "Nova · Experto",
                "No hay una petición candidata en memoria. Pide primero una tarea/diagnóstico o usa «consulta la API gratuita sobre ...».",
                parent=self.expert_window or self.root,
            )
            return
        if self.expert_free_button is not None:
            self.expert_free_button.configure(state="disabled")
        self.status_var.set("Consultando segunda opinión gratuita…")

        def worker():
            result = service.ask_free(trigger="ui_explicit")

            def done():
                if self.expert_free_button is not None:
                    self.expert_free_button.configure(state="normal")
                if result.get("ok"):
                    analysis = str(result.get("analysis") or result.get("response") or "")
                    check = str(result.get("recommended_next_check") or "")
                    text = (
                        f"{result.get('provider')} / {result.get('model')} · veredicto {result.get('verdict')}\n\n"
                        f"{analysis}"
                    )
                    if check:
                        text += "\n\nSiguiente comprobación: " + check
                    messagebox.showinfo("Nova · Segunda opinión", text[:5000], parent=self.expert_window or self.root)
                    self.status_var.set("Segunda opinión recibida")
                else:
                    missing = [x for x in result.get("attempts") or [] if x.get("error") == "api_key_missing"]
                    if missing:
                        messagebox.showwarning(
                            "Nova · API gratuita",
                            "No hay API key configurada. Recomendado: define CEREBRAS_API_KEY y reinicia Nova. Alternativa: GROQ_API_KEY.",
                            parent=self.expert_window or self.root,
                        )
                    else:
                        messagebox.showerror(
                            "Nova · API gratuita",
                            f"No pude obtener la segunda opinión: {result.get('error') or 'error del proveedor'}",
                            parent=self.expert_window or self.root,
                        )
                    self.status_var.set("Segunda opinión no disponible")
                self._refresh_expert_manager()

            self.root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def show_manager(self):
        if self.expert_window is not None:
            try:
                if self.expert_window.winfo_exists():
                    self.expert_window.deiconify()
                    self.expert_window.lift()
                    self._refresh_expert_manager()
                    return
            except Exception:
                pass
        win = tk.Toplevel(self.root)
        self.expert_window = win
        win.title("Nova · Expert Escalation")
        win.geometry("760x470")
        win.minsize(620, 360)

        head = tk.Frame(win, padx=10, pady=8)
        head.pack(fill="x")
        tk.Label(head, text="🧠 Expert Escalation", font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Button(head, text="Actualizar", command=self._refresh_expert_manager).pack(side="right")

        text = tk.Text(win, wrap="word", font=("Segoe UI", 10), padx=10, pady=10)
        text.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        text.configure(state="disabled")
        self.expert_status_text = text

        controls = tk.Frame(win, padx=10, pady=10)
        controls.pack(fill="x")
        button = tk.Button(controls, text="⚡ API gratis", command=self._expert_free_opinion, width=14)
        button.pack(side="left")
        self.expert_free_button = button
        tk.Button(controls, text="💬 Preparar ChatGPT", command=self._expert_prepare_chatgpt, width=19).pack(
            side="left", padx=(8, 0)
        )
        tk.Label(
            controls,
            text="ChatGPT Assisted requiere que tú pulses Enviar y luego Copiar.",
            anchor="w",
        ).pack(side="left", padx=(12, 0))

        win.protocol("WM_DELETE_WINDOW", lambda: win.withdraw())
        self._refresh_expert_manager()

    UI.__init__ = init
    UI._build = build
    UI.show_expert_manager = show_manager
    UI._expert_format_status = format_status
    UI._refresh_expert_manager = refresh
    UI._expert_prepare_chatgpt = prepare_chatgpt
    UI._expert_free_opinion = free_opinion
    UI._nova_expert_patched = True
    return mod
