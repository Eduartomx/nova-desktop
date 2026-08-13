from __future__ import annotations

"""Estado y controles de Gaming Awareness para la UI de Nova."""

from .config import save_config
from .gaming_awareness import get_gaming_awareness


def _gaming_label(report):
    if not report.get("enabled", True):
        return "🎮 Juego: desactivado"
    mode = str(report.get("mode") or "auto")
    if mode == "off":
        return "🎮 Juego: manual off"
    if report.get("active"):
        game = report.get("game") or {}
        name = str(game.get("process") or "Gaming Mode")
        if report.get("llm_released"):
            return f"🎮 {name} · VRAM priorizada"
        if report.get("keep_llm_loaded_during_game"):
            return f"🎮 {name} · Qwen mantenido"
        return f"🎮 {name} · Gaming Mode"
    return "🎮 Juego: auto" if mode == "auto" else "🎮 Juego: preparado"


def _warm_label(report):
    if report.get("warming"):
        return "LLM: precargando…"
    if report.get("loaded"):
        vram = float(report.get("size_vram_mb") or 0)
        return f"LLM: listo · {vram:.0f} MB VRAM" if vram else "LLM: listo"
    if report.get("last_error"):
        return "LLM: precarga no disponible"
    return "LLM: descargado"


def install_ui_gaming():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_gaming_awareness_patched", False):
        return mod

    tk = mod.tk
    original_init = UI.__init__
    original_close = UI._close
    original_doctor_render = getattr(UI, "_doctor_render", None)

    def init(self, *args, **kwargs):
        self._gaming_window = None
        original_init(self, *args, **kwargs)
        self.gaming_awareness = getattr(self.agent, "gaming_awareness", None) or get_gaming_awareness(self.config)

        controls = tk.Frame(self.root, padx=12)
        packed = self.root.pack_slaves()
        before = packed[-1] if packed else None
        pack_args = {"fill": "x", "pady": (0, 6)}
        if before is not None:
            pack_args["before"] = before
        controls.pack(**pack_args)
        self.gaming_mode_var = tk.StringVar(value=_gaming_label(self.gaming_awareness.status(refresh=False)))
        tk.Label(controls, textvariable=self.gaming_mode_var, anchor="w", fg="#666").pack(side="left")
        tk.Button(controls, text="🎮 Juego", command=self._open_gaming_settings).pack(side="right")

        try:
            self.gaming_awareness.start()
        except Exception:
            pass
        try:
            self.root.after(800, self._gaming_ui_tick)
        except Exception:
            pass

    def _gaming_ui_tick(self):
        if getattr(self, "_closing", False):
            return
        try:
            manager = getattr(self, "gaming_awareness", None)
            if manager is not None:
                report = manager.status(refresh=False)
                self.gaming_mode_var.set(_gaming_label(report))
                if hasattr(self, "llm_warm_var"):
                    if report.get("active") and report.get("llm_released"):
                        self.llm_warm_var.set("LLM: liberado · Gaming Mode")
                    else:
                        warm = getattr(self, "llm_warm_manager", None)
                        if warm is not None:
                            self.llm_warm_var.set(_warm_label(warm.cached_status()))
        except Exception:
            pass
        try:
            self.root.after(1000, self._gaming_ui_tick)
        except Exception:
            pass

    def _open_gaming_settings(self):
        if self._gaming_window is not None:
            try:
                if self._gaming_window.winfo_exists():
                    self._gaming_window.deiconify()
                    self._gaming_window.lift()
                    return
            except Exception:
                pass

        manager = self.gaming_awareness
        report = manager.status(refresh=False)
        cfg = self.config.setdefault("gaming_awareness", {})

        win = tk.Toplevel(self.root)
        self._gaming_window = win
        win.title("Nova · Gaming Awareness")
        win.geometry("560x405")
        win.resizable(False, False)
        win.transient(self.root)

        body = tk.Frame(win, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Gaming Awareness", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(
            body,
            text=(
                "Nova detecta juegos mediante metadatos locales y procesos. No lee memoria del juego, "
                "no inyecta código y no usa screenshots para detectar que estás jugando."
            ),
            justify="left",
            wraplength=515,
        ).pack(anchor="w", pady=(4, 12))

        mode_var = tk.StringVar(value=str(report.get("mode") or "auto"))
        mode_frame = tk.LabelFrame(body, text="Modo actual", padx=10, pady=8)
        mode_frame.pack(fill="x")
        for text, value in (("Automático", "auto"), ("Forzar modo juego", "on"), ("Desactivar manualmente", "off")):
            tk.Radiobutton(mode_frame, text=text, value=value, variable=mode_var, command=lambda: manager.set_mode(mode_var.get())).pack(anchor="w")

        options = tk.LabelFrame(body, text="Política de recursos", padx=10, pady=8)
        options.pack(fill="x", pady=(10, 0))
        auto_var = tk.BooleanVar(value=bool(cfg.get("auto_detect", True)))
        keep_var = tk.BooleanVar(value=bool(cfg.get("keep_llm_loaded_during_game", False)))
        policy_var = tk.StringVar(value=str(cfg.get("release_policy", "smart")))
        tk.Checkbutton(options, text="Detectar juegos automáticamente", variable=auto_var).pack(anchor="w")
        tk.Checkbutton(options, text="Mantener Qwen cargado durante juegos (usa más VRAM)", variable=keep_var).pack(anchor="w")

        row = tk.Frame(options)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text="Liberación de VRAM:").pack(side="left")
        tk.OptionMenu(row, policy_var, "smart", "always", "never").pack(side="left", padx=(8, 0))
        tk.Label(
            options,
            text="smart: libera Qwen si el juego está al frente o hay presión de VRAM.",
            fg="#666",
        ).pack(anchor="w", pady=(4, 0))

        status_var = tk.StringVar(value="")
        tk.Label(body, textvariable=status_var, anchor="w", fg="#8a3b12").pack(fill="x", pady=(8, 0))
        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))

        def save():
            policy = str(policy_var.get() or "smart")
            if policy not in {"smart", "always", "never"}:
                status_var.set("Política inválida.")
                return
            cfg["auto_detect"] = bool(auto_var.get())
            cfg["release_policy"] = policy
            cfg["keep_llm_loaded_during_game"] = bool(keep_var.get())
            manager.update_config(self.config)
            manager.set_keep_llm_loaded(bool(keep_var.get()))
            try:
                save_config(self.config)
            except Exception as exc:
                status_var.set("No pude guardar config.json: " + str(exc))
                return
            try:
                manager.tick()
            except Exception:
                pass
            win.destroy()

        tk.Button(buttons, text="Cerrar", command=win.destroy).pack(side="right")
        tk.Button(buttons, text="Guardar", command=save, width=10).pack(side="right", padx=(0, 8))

    def _doctor_render(self, report):
        if callable(original_doctor_render):
            original_doctor_render(self, report)
        widget = getattr(self, "doctor_text", None)
        manager = getattr(self, "gaming_awareness", None)
        if widget is None or manager is None:
            return
        try:
            gaming = manager.status(refresh=False)
            widget.configure(state="normal")
            widget.insert("end", "\n\n" + manager.format_status(gaming))
            widget.configure(state="disabled")
        except Exception:
            pass

    def _close(self):
        manager = getattr(self, "gaming_awareness", None)
        if manager is not None:
            try:
                manager.stop(timeout=0.4)
            except Exception:
                pass
        return original_close(self)

    UI.__init__ = init
    UI._gaming_ui_tick = _gaming_ui_tick
    UI._open_gaming_settings = _open_gaming_settings
    if callable(original_doctor_render):
        UI._doctor_render = _doctor_render
    UI._close = _close
    UI._nova_gaming_awareness_patched = True
    return mod
