from __future__ import annotations

"""Gestión del estado caliente del LLM local de Nova.

La precarga usa una petición vacía a ``/api/chat`` de Ollama. No envía prompts,
no genera contenido y no usa servicios externos. El mismo ``keep_alive`` se
aplica a las inferencias normales para evitar cold starts innecesarios.

Desde 0.9.5 el manager acepta overrides de ejecución para Gaming Awareness: se
puede suspender la precarga, reducir temporalmente keep_alive y diferir una
descarga mientras exista una inferencia activa.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests


DEFAULT_LLM_WARM_CONFIG: dict[str, Any] = {
    "enabled": True,
    "preload_on_start": True,
    "startup_delay_seconds": 0.35,
    "keep_alive": "20m",
    "request_timeout_seconds": 30.0,
    "status_timeout_seconds": 2.5,
    "unload_on_exit": True,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mb(value: Any) -> float:
    try:
        return round(float(value or 0) / (1024.0 * 1024.0), 1)
    except Exception:
        return 0.0


class LLMWarmManager:
    def __init__(self, config: dict[str, Any] | None = None):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._warming = False
        self._active_inferences = 0
        self._runtime_keep_alive_override: str | int | float | None = None
        self._runtime_keep_alive_reason = ""
        self._preload_suppressed_by = ""
        self._last_preload_at = ""
        self._last_preload_ms = 0.0
        self._last_error = ""
        self._last_loaded: bool | None = None
        self._last_size_vram_mb = 0.0
        self._last_expires_at = ""
        self._last_unload_reason = ""
        self.update_config(config or {})

    def update_config(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        warm_cfg = config.get("llm_warm", {}) if isinstance(config, dict) else {}
        merged = dict(DEFAULT_LLM_WARM_CONFIG)
        if isinstance(warm_cfg, dict):
            merged.update(warm_cfg)
        with self._lock:
            self.config = config
            self.warm_config = merged
            self.model = str(config.get("model") or "qwen3.5:4b")
            self.ollama_host = str(config.get("ollama_host") or "http://127.0.0.1:11434").rstrip("/")
            self.context_tokens = max(512, int(config.get("context_tokens", 8192) or 8192))

    @property
    def enabled(self) -> bool:
        return bool(self.warm_config.get("enabled", True))

    @property
    def preload_on_start(self) -> bool:
        return self.enabled and bool(self.warm_config.get("preload_on_start", True))

    @property
    def unload_on_exit(self) -> bool:
        return self.enabled and bool(self.warm_config.get("unload_on_exit", True))

    @property
    def keep_alive(self) -> str | int | float:
        value = self.warm_config.get("keep_alive", "20m")
        if isinstance(value, (int, float)):
            return value
        text = str(value or "20m").strip()
        return text or "20m"

    @property
    def effective_keep_alive(self) -> str | int | float:
        with self._lock:
            if self._runtime_keep_alive_override is not None:
                return self._runtime_keep_alive_override
        return self.keep_alive

    def set_runtime_keep_alive_override(self, value: str | int | float, reason: str = "runtime") -> None:
        if not isinstance(value, (str, int, float)):
            raise TypeError("keep_alive runtime debe ser str, int o float")
        with self._lock:
            self._runtime_keep_alive_override = value
            self._runtime_keep_alive_reason = str(reason or "runtime")[:120]

    def clear_runtime_keep_alive_override(self, reason: str | None = None) -> None:
        with self._lock:
            if reason and self._runtime_keep_alive_reason and self._runtime_keep_alive_reason != str(reason):
                return
            self._runtime_keep_alive_override = None
            self._runtime_keep_alive_reason = ""

    def suppress_preload(self, reason: str = "runtime") -> None:
        with self._lock:
            self._preload_suppressed_by = str(reason or "runtime")[:120]

    def clear_preload_suppression(self, reason: str | None = None) -> None:
        with self._lock:
            if reason and self._preload_suppressed_by and self._preload_suppressed_by != str(reason):
                return
            self._preload_suppressed_by = ""

    def begin_inference(self) -> None:
        with self._lock:
            self._active_inferences += 1

    def end_inference(self) -> None:
        with self._lock:
            self._active_inferences = max(0, self._active_inferences - 1)

    def apply_keep_alive(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.enabled:
            payload["keep_alive"] = self.effective_keep_alive
        return payload

    def _model_matches(self, candidate: Any) -> bool:
        current = str(candidate or "").strip().casefold()
        target = self.model.strip().casefold()
        if not current or not target:
            return False
        if current == target:
            return True
        if ":" not in target and current == target + ":latest":
            return True
        return False

    def cached_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": not bool(self._last_error),
                "enabled": self.enabled,
                "preload_on_start": self.preload_on_start,
                "warming": self._warming,
                "active_inferences": self._active_inferences,
                "model": self.model,
                "keep_alive": self.keep_alive,
                "effective_keep_alive": self.effective_keep_alive,
                "runtime_keep_alive_reason": self._runtime_keep_alive_reason,
                "preload_suppressed_by": self._preload_suppressed_by,
                "loaded": self._last_loaded,
                "size_vram_mb": self._last_size_vram_mb,
                "expires_at": self._last_expires_at,
                "last_preload_at": self._last_preload_at,
                "last_preload_ms": self._last_preload_ms,
                "last_unload_reason": self._last_unload_reason,
                "last_error": self._last_error,
            }

    def status(self, refresh: bool = True) -> dict[str, Any]:
        if not refresh:
            return self.cached_status()
        try:
            response = requests.get(
                self.ollama_host + "/api/ps",
                timeout=float(self.warm_config.get("status_timeout_seconds", 2.5) or 2.5),
                headers={"User-Agent": "Nova-Warm/0.9.5"},
            )
            response.raise_for_status()
            data = response.json()
            models = data.get("models", []) if isinstance(data, dict) else []
            match = None
            for row in models if isinstance(models, list) else []:
                if not isinstance(row, dict):
                    continue
                if self._model_matches(row.get("model") or row.get("name")):
                    match = row
                    break
            with self._lock:
                self._last_loaded = bool(match)
                self._last_size_vram_mb = _mb((match or {}).get("size_vram"))
                self._last_expires_at = str((match or {}).get("expires_at") or "")
            return self.cached_status()
        except Exception as exc:
            status = self.cached_status()
            status.update({
                "ok": False,
                "reachable": False,
                "status_error": f"{type(exc).__name__}: {exc}",
            })
            return status

    def preload(self, reason: str = "manual") -> dict[str, Any]:
        if not self.enabled:
            return self.cached_status()

        with self._lock:
            if self._preload_suppressed_by:
                report = self.cached_status()
                report["preload_skipped_reason"] = self._preload_suppressed_by
                return report
            self._warming = True
            self._last_error = ""
        started = time.perf_counter()
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [],
                "stream": False,
                "keep_alive": self.effective_keep_alive,
                "options": {"num_ctx": self.context_tokens},
            }
            response = requests.post(
                self.ollama_host + "/api/chat",
                json=payload,
                timeout=float(self.warm_config.get("request_timeout_seconds", 30.0) or 30.0),
                headers={"User-Agent": "Nova-Warm/0.9.5"},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Ollama devolvió una respuesta no estructurada al precargar.")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._lock:
                self._last_preload_at = _utc_now()
                self._last_preload_ms = round(elapsed_ms, 2)
                self._last_loaded = True
                self._last_error = ""
                if str(reason or "") != "gaming_restore":
                    self._last_unload_reason = ""
            report = self.status(refresh=True)
            if report.get("reachable") is False:
                report["ok"] = True
                report["loaded"] = True
            return report
        except Exception as exc:
            with self._lock:
                self._last_preload_ms = round((time.perf_counter() - started) * 1000.0, 2)
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._last_loaded = False
            return self.cached_status()
        finally:
            with self._lock:
                self._warming = False

    def unload(self, timeout: float | None = None, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return self.cached_status()
        with self._lock:
            if not force and (self._warming or self._active_inferences > 0):
                report = self.cached_status()
                report["unload_deferred"] = True
                report["unload_deferred_reason"] = "warming" if self._warming else "active_inference"
                return report
        try:
            response = requests.post(
                self.ollama_host + "/api/chat",
                json={"model": self.model, "messages": [], "stream": False, "keep_alive": 0},
                timeout=float(timeout if timeout is not None else self.warm_config.get("status_timeout_seconds", 2.5) or 2.5),
                headers={"User-Agent": "Nova-Warm/0.9.5"},
            )
            response.raise_for_status()
            with self._lock:
                self._last_loaded = False
                self._last_size_vram_mb = 0.0
                self._last_expires_at = ""
                self._last_error = ""
                self._last_unload_reason = str(reason or "manual")[:120]
            return self.cached_status()
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            return self.cached_status()

    def start_background(
        self,
        callback: Callable[[dict[str, Any]], None] | None = None,
        reason: str = "startup",
    ) -> threading.Thread | None:
        if not self.preload_on_start:
            return None
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread

            delay = max(0.0, float(self.warm_config.get("startup_delay_seconds", 0.35) or 0.0))

            def worker():
                if delay:
                    time.sleep(delay)
                report = self.preload(reason=reason)
                if callback is not None:
                    try:
                        callback(report)
                    except Exception:
                        pass

            self._thread = threading.Thread(target=worker, daemon=True, name="nova-llm-warmup")
            self._thread.start()
            return self._thread


_INSTANCE: LLMWarmManager | None = None


def get_llm_warm_manager(config: dict[str, Any] | None = None) -> LLMWarmManager:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = LLMWarmManager(config or {})
    elif isinstance(config, dict):
        _INSTANCE.update_config(config)
    return _INSTANCE
