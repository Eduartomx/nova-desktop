from __future__ import annotations

"""UI base administrada por GitHub desde Nova 0.9.0.

Las capas `ui_*.py` continúan añadiendo Workspaces, Doctor, Skills, Experto,
Perception y otros paneles. Este archivo conserva el contrato estable que esas
capas necesitan y elimina la dependencia de una UI histórica no recuperable.
"""

import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox

from .agent import LocalAgent

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AssistantUI:
    def __init__(self, root: tk.Tk, config: dict[str, Any] | None = None):
        self.root = root
        self.config = config or {}
        self.name = str(self.config.get("assistant_name") or "Nova")
        self.agent = LocalAgent(self.config)
        self.busy = False
        self.result_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._hotkey_listener = None
        self._ptt_listener = None
        self._audio_stream = None
        self._audio_chunks: list[Any] = []
        self._recording = False
        self._whisper_model = None
        self._closing = False

        self.root.title(f"{self.name} · Asistente local")
        self.root.geometry("820x620")
        self.root.minsize(560, 420)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self._build()
        self._install_hotkeys()
        self.root.after(100, self._poll_results)

    def _build(self):
        header = tk.Frame(self.root, padx=12, pady=10)
        header.pack(fill="x")
        tk.Label(header, text=self.name, font=("Segoe UI", 16, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="Listo")
        tk.Label(header, textvariable=self.status_var, anchor="e").pack(side="right")

        self.chat = tk.Text(self.root, wrap="word", state="disabled", font=("Segoe UI", 10), padx=10, pady=10)
        self.chat.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        composer = tk.Frame(self.root, padx=12, pady=8)
        composer.pack(fill="x")
        self.input_var = tk.StringVar()
        self.input_entry = tk.Entry(composer, textvariable=self.input_var, font=("Segoe UI", 11))
        self.input_entry.pack(side="left", fill="x", expand=True)
        self.input_entry.bind("<Return>", lambda _event: self._send_from_entry())
        self.mic_button = tk.Button(composer, text="🎙 F9", command=self._toggle_recording, width=8)
        self.mic_button.pack(side="left", padx=(8, 0))
        self.send_button = tk.Button(composer, text="Enviar", command=self._send_from_entry, width=10)
        self.send_button.pack(side="left", padx=(8, 0))

        foot = tk.Frame(self.root, padx=12, pady=(0, 8))
        foot.pack(fill="x")
        hotkey = str(self.config.get("hotkey", "<ctrl>+<alt>+<space>"))
        tk.Label(foot, text=f"Atajo: {hotkey} · Contexto: Ctrl+Shift+Espacio · Voz: F9", anchor="w", fg="#666").pack(fill="x")

        self._append("system", "Nova Core 0.9 · Agent/Tools/UI administrados por GitHub; las acciones siguen respetando seguridad y verificaciones.")
        try:
            self.input_entry.focus_set()
        except Exception:
            pass

    def _append(self, role: str, text: str):
        label = {"user": "Tú", "assistant": self.name, "system": "Sistema", "error": "Error"}.get(str(role), str(role).title())
        try:
            self.chat.configure(state="normal")
            if self.chat.index("end-1c") != "1.0":
                self.chat.insert("end", "\n\n")
            self.chat.insert("end", f"{label}: {str(text or '')}")
            self.chat.see("end")
            self.chat.configure(state="disabled")
        except Exception:
            pass

    def _send_from_entry(self):
        text = self.input_var.get().strip()
        if not text:
            return
        self.input_var.set("")
        self._submit_text(text)

    def _submit_text(self, text: str):
        if self.busy:
            self.status_var.set("Nova todavía está trabajando…")
            return
        text = str(text or "").strip()
        if not text:
            return
        self.busy = True
        self.send_button.configure(state="disabled")
        self.mic_button.configure(state="disabled")
        self.status_var.set("Pensando…")
        self._append("user", text)

        def worker():
            try:
                answer = self.agent.ask(text)
                self.result_queue.put(("answer", str(answer or "")))
            except Exception as exc:
                self.result_queue.put(("error", f"{exc}\n{traceback.format_exc(limit=4)}"))

        threading.Thread(target=worker, daemon=True, name="nova-agent-ui").start()

    # Alias histórico usado por algunas UI locales.
    _send = _send_from_entry

    def _poll_results(self):
        if self._closing:
            return
        try:
            while True:
                kind, text = self.result_queue.get_nowait()
                if kind == "answer":
                    self._append("assistant", text)
                    self.status_var.set("Listo")
                    self._speak_async(text)
                else:
                    self._append("error", text)
                    self.status_var.set("Error")
                self.busy = False
                try:
                    self.send_button.configure(state="normal")
                    self.mic_button.configure(state="normal")
                    self.input_entry.focus_set()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._poll_results)
        except Exception:
            pass

    # ---------- ventana / hotkeys ----------
    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(120, lambda: self.root.attributes("-topmost", False))
            self.root.after(0, self.input_entry.focus_set)
        except Exception:
            pass

    def _context_hotkey(self):
        self._show_window()
        try:
            self.input_var.set("¿Qué estaba usando antes de abrirte y qué contexto relevante ves?")
            self.input_entry.icursor("end")
        except Exception:
            pass

    @staticmethod
    def _normalize_hotkey(value: str) -> str:
        raw = str(value or "").strip().casefold()
        raw = raw.replace("ctrl", "ctrl").replace("control", "ctrl")
        if not raw.startswith("<"):
            parts = [x.strip(" <>+") for x in raw.split("+") if x.strip(" <>+")]
            raw = "+".join(f"<{x}>" if len(x) > 1 else x for x in parts)
        return raw or "<ctrl>+<alt>+<space>"

    def _install_hotkeys(self):
        try:
            from pynput import keyboard
            main_hotkey = self._normalize_hotkey(str(self.config.get("hotkey", "<ctrl>+<alt>+<space>")))
            context_hotkey = self._normalize_hotkey(str(self.config.get("desktop", {}).get("context_hotkey", "<ctrl>+<shift>+<space>")))
            self._hotkey_listener = keyboard.GlobalHotKeys({
                main_hotkey: lambda: self.root.after(0, self._show_window),
                context_hotkey: lambda: self.root.after(0, self._context_hotkey),
            })
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()

            # Listener separado para PTT porque necesitamos press + release.
            ptt_key = str(self.config.get("voice", {}).get("push_to_talk_hotkey", "<f9>"))
            if "f9" in ptt_key.casefold():
                def on_press(key):
                    if key == keyboard.Key.f9 and not self._recording:
                        self.root.after(0, self._start_recording)
                def on_release(key):
                    if key == keyboard.Key.f9 and self._recording:
                        self.root.after(0, self._stop_recording)
                self._ptt_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
                self._ptt_listener.daemon = True
                self._ptt_listener.start()
        except Exception as exc:
            try:
                self._append("system", f"Atajos globales no disponibles: {exc}")
            except Exception:
                pass

    # ---------- voz local PTT ----------
    def _toggle_recording(self):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if self._recording or self.busy or not self.config.get("voice", {}).get("enabled", True):
            return
        try:
            import sounddevice as sd
            self._audio_chunks = []
            sample_rate = 16000

            def callback(indata, frames, time_info, status):
                del frames, time_info, status
                if self._recording:
                    self._audio_chunks.append(indata.copy())

            self._recording = True
            self._audio_stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32", callback=callback)
            self._audio_stream.start()
            self.status_var.set("Escuchando… suelta F9 para enviar")
            self.mic_button.configure(text="⏹ Voz")
        except Exception as exc:
            self._recording = False
            self.status_var.set("Micrófono no disponible")
            messagebox.showwarning("Nova · Voz", f"No pude iniciar el micrófono:\n{exc}", parent=self.root)

    def _stop_recording(self):
        if not self._recording:
            return
        self._recording = False
        stream = self._audio_stream
        self._audio_stream = None
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            pass
        self.mic_button.configure(text="🎙 F9")
        chunks = list(self._audio_chunks)
        self._audio_chunks = []
        if not chunks:
            self.status_var.set("No detecté audio")
            return
        self.status_var.set("Transcribiendo localmente…")

        def worker():
            try:
                import numpy as np
                from faster_whisper import WhisperModel
                audio = np.concatenate(chunks, axis=0).reshape(-1).astype("float32")
                if self._whisper_model is None:
                    voice = self.config.get("voice", {})
                    self._whisper_model = WhisperModel(
                        str(voice.get("stt_model") or "small"),
                        device=str(voice.get("stt_device") or "cpu"),
                        compute_type=str(voice.get("stt_compute_type") or "int8"),
                    )
                segments, _info = self._whisper_model.transcribe(
                    audio,
                    language=str(self.config.get("voice", {}).get("language") or "es"),
                    vad_filter=True,
                )
                text = " ".join(str(seg.text).strip() for seg in segments if str(seg.text).strip()).strip()
                self.root.after(0, lambda t=text: self._voice_transcribed(t))
            except Exception as exc:
                self.root.after(0, lambda err=str(exc): self.status_var.set(f"Error STT: {err[:120]}"))

        threading.Thread(target=worker, daemon=True, name="nova-stt").start()

    def _voice_transcribed(self, text: str):
        if not text:
            self.status_var.set("No entendí audio")
            return
        self.input_var.set(text)
        self.status_var.set("Voz transcrita")
        self._send_from_entry()

    def _speak_async(self, text: str):
        voice = self.config.get("voice", {}) if isinstance(self.config, dict) else {}
        if not voice.get("tts_enabled", True) or not str(text or "").strip():
            return
        def worker():
            try:
                import pyttsx3
                engine = pyttsx3.init()
                engine.say(str(text)[:4000])
                engine.runAndWait()
                engine.stop()
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True, name="nova-tts").start()

    def _close(self):
        if self._closing:
            return
        self._closing = True
        try:
            if self._recording:
                self._stop_recording()
        except Exception:
            pass
        for listener in (self._hotkey_listener, self._ptt_listener):
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
