from __future__ import annotations

"""Controles de Instant Wake y edición en vivo de hotkeys para la UI."""

from typing import Any

from .config import save_config
from .hotkeys import (
    DEFAULT_CONTEXT_HOTKEY,
    DEFAULT_MAIN_HOTKEY,
    humanize_hotkey,
    normalize_hotkey,
    validate_hotkey,
)
from .llm_warm import get_llm_warm_manager


def _warm_label(report: dict[str, Any]) -> str:
    if not report.get("enabled", True):
        return "LLM: warm desactivado"
    if report.get("warming"):
        return "LLM: precargando…"
    if report.get("loaded"):
        vram = float(report.get("size_vram_mb") or 0)
        return f"LLM: listo · {vram:.0f} MB VRAM" if vram else "LLM: listo"
    if report.get("last_error"):
        return "LLM: precarga no disponible"
    return "LLM: descargado"


def _warm_doctor_text(report: dict[str, Any]) -> str:
    state = "precargando" if report.get("warming") else ("cargado" if report.get("loaded") else "descargado")
    lines = [
        "LLM Warm Manager",
        f"- Estado: {state} · modelo {report.get('model', '?')} · keep-alive {report.get('keep_alive', '?')}",
    ]
    if report.get("size_vram_mb"):
        lines.append(f"- VRAM reservada por el modelo: {report.get('size_vram_mb')} MB")
    if report.get("last_preload_ms"):
        lines.append(f"- Última precarga: {report.get('last_preload_ms')} ms")
    if report.get("expires_at"):
        lines.append(f"- Expiración reportada por Ollama: {report.get('expires_at')}")
    if report.get("last_error"):
        lines.append(f"- Último error de precarga: {report.get('last_error')}")
    return "\n".join(lines)


def install_ui_instant_wake():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_instant_wake_patched", False):
        return mod

    tk = mod.tk
    original_init = UI.__init__
    original_close = UI._close
    original_doctor_render = getattr(UI, "_doctor_render", None)

    @staticmethod
    def normalize_for_compat(value: str) -> str:
        return normalize_hotkey(value, DEFAULT_MAIN_HOTKEY)

    def _install_hotkeys(self):
        try:
            from pynput import keyboard

            main_value = str(self.config.get("hotkey") or DEFAULT_MAIN_HOTKEY)
            context_value = str(self.config.get("desktop", {}).get("context_hotkey") or DEFAULT_CONTEXT_HOTKEY)
            ok_main, err_main, main_hotkey = validate_hotkey(main_value)
            ok_context, err_context, context_hotkey = validate_hotkey(context_value)
            if not ok_main:
                raise ValueError("Atajo principal: " + err_main)
            if not ok_context:
                raise ValueError("Atajo de contexto: " + err_context)
            if main_hotkey == context_hotkey:
                raise ValueError("El atajo principal y el de contexto no pueden ser iguales.")

            new_hotkeys = keyboard.GlobalHotKeys({
                main_hotkey: lambda: self.root.after(0, self._show_window),
                context_hotkey: lambda: self.root.after(0, self._context_hotkey),
            })
            new_hotkeys.daemon = True
            new_hotkeys.start()

            new_ptt = None
            ptt_key = str(self.config.get("voice", {}).get("push_to_talk_hotkey", "<f9>"))
            if "f9" in ptt_key.casefold():
                def on_press(key):
                    if key == keyboard.Key.f9 and not self._recording:
                        self.root.after(0, self._start_recording)

                def on_release(key):
                    if key == keyboard.Key.f9 and self._recording:
                        self.root.after(0, self._stop_recording)

                new_ptt = keyboard.Listener(on_press=on_press, on_release=on_release)
                new_ptt.daemon = True
                new_ptt.start()

            old_hotkeys = getattr(self, "_hotkey_listener", None)
            old_ptt = getattr(self, "_ptt_listener", None)
            self._hotkey_listener = new_hotkeys
            self._ptt_listener = new_ptt
            for listener in (old_hotkeys, old_ptt):
                try:
                    if listener is not None:
                        listener.stop()
                except Exception:
                    pass
            self._hotkey_install_error = ""
            return True, ""
        except Exception as exc:
            self._hotkey_install_error = str(exc)
            try:
                self._append("system", f"Atajos globales no disponibles: {exc}")
            except Exception:
                pass
            return False, str(exc)

    def _refresh_hotkey_labels(self):
        main_text = humanize_hotkey(self.config.get("hotkey") or DEFAULT_MAIN_HOTKEY)
        context_text = humanize_hotkey(self.config.get("desktop", {}).get("context_hotkey") or DEFAULT_CONTEXT_HOTKEY)

        def walk(widget):
            try:
                children = widget.winfo_children()
            except Exception:
                return
            for child in children:
                try:
                    if isinstance(child, tk.Label):
                        text = str(child.cget("text") or "")
                        if text.startswith("Atajo:"):
                            child.configure(text=f"Atajo: {main_text} · Contexto: {context_text} · Voz: F9")
                except Exception:
                    pass
                walk(child)

        walk(self.root)

    def _open_hotkey_settings(self):
        if getattr(self, "_hotkey_window", None) is not None:
            try:
                if self._hotkey_window.winfo_exists():
                    self._hotkey_window.deiconify()
                    self._hotkey_window.lift()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self._hotkey_window = win
        win.title("Nova · Atajos globales")
        win.geometry("500x285")
        win.resizable(False, False)
        win.transient(self.root)

        body = tk.Frame(win, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Atajos globales", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(
            body,
            text="Escribe combinaciones como Ctrl+Alt+N. Se aplican inmediatamente, sin reiniciar Nova.",
            justify="left",
            wraplength=450,
        ).pack(anchor="w", pady=(4, 14))

        main_var = tk.StringVar(value=humanize_hotkey(self.config.get("hotkey") or DEFAULT_MAIN_HOTKEY))
        context_var = tk.StringVar(value=humanize_hotkey(self.config.get("desktop", {}).get("context_hotkey") or DEFAULT_CONTEXT_HOTKEY))

        tk.Label(body, text="Abrir Nova").pack(anchor="w")
        main_entry = tk.Entry(body, textvariable=main_var, font=("Segoe UI", 10))
        main_entry.pack(fill="x", pady=(2, 10))
        tk.Label(body, text="Abrir Nova con contexto").pack(anchor="w")
        tk.Entry(body, textvariable=context_var, font=("Segoe UI", 10)).pack(fill="x", pady=(2, 12))

        status_var = tk.StringVar(value="")
        tk.Label(body, textvariable=status_var, anchor="w", fg="#8a3b12").pack(fill="x")

        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))

        def restore_defaults():
            main_var.set("Ctrl+Alt+N")
            context_var.set("Ctrl+Alt+Shift+N")
            status_var.set("Defaults de Nova 0.9.4 preparados. Pulsa Guardar para aplicarlos.")

        def save():
            ok_main, err_main, main_hotkey = validate_hotkey(main_var.get())
            ok_context, err_context, context_hotkey = validate_hotkey(context_var.get())
            if not ok_main:
                status_var.set(err_main)
                return
            if not ok_context:
                status_var.set(err_context)
                return
            if main_hotkey == context_hotkey:
                status_var.set("Los dos atajos no pueden ser iguales.")
                return

            previous_main = self.config.get("hotkey")
            desktop = self.config.setdefault("desktop", {})
            previous_context = desktop.get("context_hotkey")
            self.config["hotkey"] = main_hotkey
            desktop["context_hotkey"] = context_hotkey
            installed, error = self._install_hotkeys()
            if not installed:
                self.config["hotkey"] = previous_main
                desktop["context_hotkey"] = previous_context
                self._install_hotkeys()
                status_var.set("No pude registrar el atajo: " + error)
                return
            try:
                save_config(self.config)
            except Exception as exc:
                self.config["hotkey"] = previous_main
                desktop["context_hotkey"] = previous_context
                self._install_hotkeys()
                status_var.set("No pude guardar config.json: " + str(exc))
                return
            self._refresh_hotkey_labels()
            try:
                self._append("system", f"Atajos actualizados: {humanize_hotkey(main_hotkey)} · contexto {humanize_hotkey(context_hotkey)}")
            except Exception:
                pass
            win.destroy()

        tk.Button(buttons, text="Restaurar 0.9.4", command=restore_defaults).pack(side="left")
        tk.Button(buttons, text="Cancelar", command=win.destroy).pack(side="right")
        tk.Button(buttons, text="Guardar", command=save, width=10).pack(side="right", padx=(0, 8))
        main_entry.focus_set()

    def _warmup_finished(self, report):
        if getattr(self, "_closing", False):
            return
        try:
            self.llm_warm_var.set(_warm_label(report))
        except Exception:
            pass
        if not getattr(self, "busy", False):
            try:
                self.status_var.set("Listo" if report.get("loaded") else "Listo · LLM bajo demanda")
            except Exception:
                pass

    def init(self, *args, **kwargs):
        self._hotkey_window = None
        self._hotkey_install_error = ""
        original_init(self, *args, **kwargs)
        self.llm_warm_manager = getattr(self.agent, "llm_warm", None) or get_llm_warm_manager(self.config)

        controls = tk.Frame(self.root, padx=12)
        packed = self.root.pack_slaves()
        before = packed[-1] if packed else None
        pack_args = {"fill": "x", "pady": (0, 6)}
        if before is not None:
            pack_args["before"] = before
        controls.pack(**pack_args)
        self.llm_warm_var = tk.StringVar(value="LLM: preparando…" if self.llm_warm_manager.preload_on_start else "LLM: bajo demanda")
        tk.Label(controls, textvariable=self.llm_warm_var, anchor="w", fg="#666").pack(side="left")
        tk.Button(controls, text="⚙ Atajos", command=self._open_hotkey_settings).pack(side="right")
        self._refresh_hotkey_labels()

        if self.llm_warm_manager.preload_on_start:
            try:
                self.status_var.set("Preparando Qwen…")
            except Exception:
                pass

            def callback(report):
                try:
                    self.root.after(0, lambda r=report: self._warmup_finished(r))
                except Exception:
                    pass

            self.llm_warm_manager.start_background(callback)
        else:
            try:
                self.llm_warm_var.set(_warm_label(self.llm_warm_manager.cached_status()))
            except Exception:
                pass

    def _doctor_render(self, report):
        if callable(original_doctor_render):
            original_doctor_render(self, report)
        widget = getattr(self, "doctor_text", None)
        if widget is None:
            return
        try:
            warm = self.llm_warm_manager.status(refresh=True)
            widget.configure(state="normal")
            widget.insert("end", "\n\n" + _warm_doctor_text(warm))
            widget.configure(state="disabled")
        except Exception:
            pass

    def _close(self):
        manager = getattr(self, "llm_warm_manager", None)
        if manager is not None and manager.unload_on_exit:
            try:
                manager.unload(timeout=1.2)
            except Exception:
                pass
        return original_close(self)

    UI._normalize_hotkey = normalize_for_compat
    UI._install_hotkeys = _install_hotkeys
    UI._refresh_hotkey_labels = _refresh_hotkey_labels
    UI._open_hotkey_settings = _open_hotkey_settings
    UI._warmup_finished = _warmup_finished
    UI.__init__ = init
    if callable(original_doctor_render):
        UI._doctor_render = _doctor_render
    UI._close = _close
    UI._nova_instant_wake_patched = True
    return mod
