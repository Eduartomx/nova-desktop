from __future__ import annotations


def install_ui_skills():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_skills_patched", False):
        return mod

    tk = mod.tk
    messagebox = mod.messagebox
    original_init = UI.__init__
    original_build = UI._build

    def init(self, *args, **kwargs):
        self.skills_window = None
        self.skills_listbox = None
        self.skills_rows = []
        self.skills_detail = None
        original_init(self, *args, **kwargs)

    def build(self):
        original_build(self)
        placed = False
        try:
            for frame in self.root.winfo_children():
                if not isinstance(frame, tk.Frame):
                    continue
                buttons = [x for x in frame.winfo_children() if isinstance(x, tk.Button)]
                if any(str(button.cget("text")) == "📁 Proyectos" for button in buttons):
                    tk.Button(frame, text="🧩 Skills", command=self.show_skills_manager, width=10).pack(
                        side="right", padx=(0, 6)
                    )
                    placed = True
                    break
        except Exception:
            placed = False
        if not placed:
            bar = tk.Frame(self.root, padx=12, pady=2)
            bar.pack(fill="x", before=self.chat)
            tk.Button(bar, text="🧩 Skills", command=self.show_skills_manager, width=10).pack(side="right")

    def refresh(self):
        registry = getattr(self.agent, "skills", None)
        if registry is None:
            return
        self.skills_rows = registry.list(include_disabled=True, limit=200)
        lb = self.skills_listbox
        if lb is not None:
            lb.delete(0, "end")
            for row in self.skills_rows:
                state = "✓" if row.get("enabled") else "×"
                scope = "workspace" if row.get("workspace_id") is not None else "global"
                lb.insert(
                    "end",
                    f"{state} {row.get('name')}  [v{row.get('version')} · {row.get('trust_level')} · {scope}]",
                )
        self._skills_show_selected()

    def selected(self):
        if self.skills_listbox is None:
            return None
        sel = self.skills_listbox.curselection()
        idx = int(sel[0]) if sel else -1
        return self.skills_rows[idx] if 0 <= idx < len(self.skills_rows) else None

    def show_selected(self):
        row = self._selected_skill()
        widget = self.skills_detail
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if row:
            lines = [
                f"{row.get('name')} · v{row.get('version')}",
                f"Estado: {'activa' if row.get('enabled') else 'deshabilitada'} · confianza {row.get('trust_level')}",
                f"Origen: {row.get('source')} · ejecuciones correctas {row.get('successful_runs')} · fallidas {row.get('failed_runs')}",
                "",
                str(row.get("description") or "Sin descripción."),
                "",
                "Triggers:",
            ]
            lines += [f"- {x}" for x in (row.get("trigger_phrases") or [])]
            lines.append("\nPasos:")
            for idx, step in enumerate(row.get("steps") or [], start=1):
                lines.append(f"{idx}. {step.get('title')}: {step.get('instruction')}")
                if step.get("verify"):
                    lines.append(f"   Verifica: {step.get('verify')}")
            if row.get("verification"):
                lines.append("\nVerificación final:")
                lines += [f"- {x}" for x in row.get("verification")]
            widget.insert("1.0", "\n".join(lines))
        widget.configure(state="disabled")

    def toggle(self):
        row = self._selected_skill()
        if not row:
            self.status_var.set("Selecciona una habilidad")
            return
        try:
            updated = self.agent.skills.set_enabled(int(row["id"]), not bool(row.get("enabled")))
            self.status_var.set(
                f"Skill {updated.get('name')}: {'activa' if updated.get('enabled') else 'deshabilitada'}"
            )
            self._refresh_skills_manager()
        except Exception as exc:
            messagebox.showerror("Nova · Skills", str(exc), parent=self.skills_window or self.root)

    def show_manager(self):
        if self.skills_window is not None:
            try:
                if self.skills_window.winfo_exists():
                    self.skills_window.deiconify()
                    self.skills_window.lift()
                    self._refresh_skills_manager()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self.skills_window = win
        win.title("Nova · Skills Engine")
        win.geometry("900x520")
        win.minsize(700, 380)

        head = tk.Frame(win, padx=10, pady=8)
        head.pack(fill="x")
        tk.Label(head, text="🧩 Habilidades de Nova", font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Button(head, text="Actualizar", command=self._refresh_skills_manager).pack(side="right")

        body = tk.PanedWindow(win, orient="horizontal", sashrelief="raised")
        body.pack(fill="both", expand=True, padx=10, pady=4)
        left = tk.Frame(body)
        right = tk.Frame(body)
        body.add(left, minsize=280)
        body.add(right, minsize=360)

        lb = tk.Listbox(left, font=("Segoe UI", 10), activestyle="dotbox")
        lb.pack(fill="both", expand=True)
        self.skills_listbox = lb
        lb.bind("<<ListboxSelect>>", lambda _event: self._skills_show_selected())

        detail = tk.Text(right, wrap="word", font=("Segoe UI", 10), padx=8, pady=8)
        detail.pack(fill="both", expand=True)
        detail.configure(state="disabled")
        self.skills_detail = detail

        controls = tk.Frame(win, padx=10, pady=10)
        controls.pack(fill="x")
        tk.Button(controls, text="✓ / × Habilitar", command=self._skills_toggle_selected).pack(side="left")
        tk.Label(
            controls,
            text="Para crear o actualizar una Skill, pídeselo a Nova en lenguaje natural.",
            anchor="w",
        ).pack(side="left", padx=(12, 0))

        win.protocol("WM_DELETE_WINDOW", lambda: win.withdraw())
        self._refresh_skills_manager()

    UI.__init__ = init
    UI._build = build
    UI.show_skills_manager = show_manager
    UI._refresh_skills_manager = refresh
    UI._selected_skill = selected
    UI._skills_show_selected = show_selected
    UI._skills_toggle_selected = toggle
    UI._nova_skills_patched = True
    return mod
