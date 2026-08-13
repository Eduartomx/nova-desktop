from __future__ import annotations

"""Wake word local para la UI nativa.

No descarga modelos automáticamente. Si el modelo preentrenado local no está
presente, Nova sigue funcionando con F9/hotkeys y muestra un aviso local.
"""

import threading
import time
from pathlib import Path


def install_ui_voice_wake():
    from . import ui as mod

    UI = mod.AssistantUI
    if getattr(UI, "_nova_voice_wake", False):
        return mod

    original_init = UI.__init__
    original_close = UI._close

    def init(self, *args, **kwargs):
        self._wake_stop = threading.Event()
        self._wake_thread = None
        self._wake_model_error = ""
        self._wake_last_activation = 0.0
        original_init(self, *args, **kwargs)
        voice = self.config.get("voice", {}) if isinstance(self.config, dict) else {}
        if voice.get("enabled", True) and voice.get("wakeword_enabled", True):
            self._wake_thread = threading.Thread(target=self._wake_loop, daemon=True, name="nova-wakeword")
            self._wake_thread.start()

    @staticmethod
    def _resolve_wake_model(name: str):
        try:
            import openwakeword
            models = getattr(openwakeword, "MODELS", {}) or {}
            spec = models.get(str(name or "hey_jarvis")) or models.get("hey_jarvis")
            if not isinstance(spec, dict):
                return None
            base = Path(str(spec.get("model_path") or ""))
            # Windows usa ONNX Runtime en las versiones modernas de openWakeWord.
            candidates = [base.with_suffix(".onnx"), base]
            for path in candidates:
                if path.is_file():
                    return path
        except Exception:
            pass
        return None

    def wake_loop(self):
        voice = self.config.get("voice", {}) if isinstance(self.config, dict) else {}
        threshold = float(voice.get("wakeword_threshold", 0.55) or 0.55)
        wake_name = str(voice.get("wakeword") or "hey_jarvis")
        model_path = self._resolve_wake_model(wake_name)
        if model_path is None:
            self._wake_model_error = f"No encontré el modelo local de wake word {wake_name}."
            try:
                self.root.after(0, lambda: self._append("system", self._wake_model_error + " F9 continúa disponible."))
            except Exception:
                pass
            return
        try:
            import numpy as np
            import sounddevice as sd
            from openwakeword.model import Model

            framework = "onnx" if model_path.suffix.casefold() == ".onnx" else "tflite"
            model = Model(wakeword_models=[str(model_path)], inference_framework=framework)
            while not self._wake_stop.is_set():
                if self._recording or self.busy:
                    time.sleep(0.15)
                    continue
                try:
                    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280) as stream:
                        while not self._wake_stop.is_set() and not self._recording and not self.busy:
                            frame, _overflow = stream.read(1280)
                            audio = np.asarray(frame).reshape(-1).astype(np.int16, copy=False)
                            predictions = model.predict(audio)
                            score = 0.0
                            if isinstance(predictions, dict):
                                for key, value in predictions.items():
                                    if wake_name.casefold().replace("_", " ") in str(key).casefold().replace("_", " ") or "jarvis" in str(key).casefold():
                                        try:
                                            score = max(score, float(value))
                                        except Exception:
                                            pass
                            if score >= threshold:
                                now = time.monotonic()
                                if now - self._wake_last_activation > 2.5:
                                    self._wake_last_activation = now
                                    try:
                                        self.root.after(0, self._wake_activate)
                                    except Exception:
                                        pass
                                break
                except Exception as exc:
                    self._wake_model_error = str(exc)[:300]
                    time.sleep(2.0)
                # Deja libre el dispositivo mientras se captura el comando.
                while not self._wake_stop.is_set() and (self._recording or self.busy):
                    time.sleep(0.15)
                time.sleep(0.35)
        except Exception as exc:
            self._wake_model_error = str(exc)[:300]
            try:
                self.root.after(0, lambda: self._append("system", f"Wake word no disponible: {self._wake_model_error}. F9 continúa disponible."))
            except Exception:
                pass

    def wake_activate(self):
        if self._closing:
            return
        try:
            self._show_window()
            self._append("system", "Wake word detectado · escuchando comando…")
            if not self.busy and not self._recording:
                self._start_recording()
                seconds = max(3, min(int(self.config.get("voice", {}).get("max_command_seconds", 15) or 15), 30))
                self.root.after(seconds * 1000, lambda: self._stop_recording() if self._recording else None)
        except Exception:
            pass

    def close(self):
        try:
            self._wake_stop.set()
        except Exception:
            pass
        return original_close(self)

    UI.__init__ = init
    UI._wake_loop = wake_loop
    UI._wake_activate = wake_activate
    UI._resolve_wake_model = _resolve_wake_model
    UI._close = close
    UI._nova_voice_wake = True
    return mod
