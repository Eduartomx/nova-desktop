from __future__ import annotations

"""Integración del LLM Warm Manager con el Agent nativo de Nova."""

import re
import time
import unicodedata
from typing import Any

import requests

from .llm_performance import get_llm_performance
from .llm_warm import get_llm_warm_manager


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def warm_direct_intent(text: str) -> str | None:
    t = _normalize(text)
    if not t:
        return None
    if any(cue in t for cue in (
        "libera la vram", "libera vram", "descarga qwen", "descarga el modelo",
        "saca qwen de la vram", "libera qwen", "enfria el llm",
    )):
        return "unload"
    if any(cue in t for cue in (
        "precarga qwen", "precarga el modelo", "carga qwen", "carga el llm",
        "calienta qwen", "calienta el llm", "prepara qwen", "prepara el llm",
    )):
        return "preload"
    if any(cue in t for cue in (
        "qwen esta cargado", "qwen esta en vram", "modelo esta cargado",
        "estado del llm warm", "estado de llm warm", "llm esta caliente",
        "estado de qwen en vram", "estado del modelo en vram",
    )):
        return "status"
    return None


def _format_warm_status(report: dict[str, Any]) -> str:
    if not report.get("enabled", True):
        return "LLM Warm Manager está desactivado en la configuración."
    if report.get("warming"):
        state = "precargando"
    elif report.get("loaded") is True:
        state = "cargado y listo"
    elif report.get("loaded") is False:
        state = "descargado"
    else:
        state = "estado aún no comprobado"
    lines = [
        f"LLM Warm Manager · {report.get('model', '?')}",
        f"- Estado: {state}",
        f"- Keep-alive: {report.get('effective_keep_alive', report.get('keep_alive', '?'))}",
    ]
    if report.get("runtime_keep_alive_reason"):
        lines.append(f"- Política temporal: {report.get('runtime_keep_alive_reason')}")
    if report.get("preload_suppressed_by"):
        lines.append(f"- Precarga suspendida por: {report.get('preload_suppressed_by')}")
    if report.get("active_inferences"):
        lines.append(f"- Inferencias activas: {report.get('active_inferences')}")
    if report.get("size_vram_mb"):
        lines.append(f"- VRAM del modelo: {report.get('size_vram_mb')} MB")
    if report.get("last_preload_ms"):
        lines.append(f"- Última precarga: {report.get('last_preload_ms')} ms")
    if report.get("expires_at"):
        lines.append(f"- Ollama lo mantendrá hasta: {report.get('expires_at')}")
    if report.get("last_error"):
        lines.append(f"- Último error: {report.get('last_error')}")
    return "\n".join(lines)


def install_agent_instant_wake():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_instant_wake_patched", False):
        return mod

    original_init = Agent.__init__
    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.llm_warm = get_llm_warm_manager(self.config)

    def ollama_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        options_override: dict[str, Any] | None = None,
        performance_label: str = "",
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"num_ctx": int(self.config.get("context_tokens", 8192))}
        if isinstance(options_override, dict):
            for key, value in options_override.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    options[str(key)] = value
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if tools:
            payload["tools"] = tools

        warm = getattr(self, "llm_warm", None) or get_llm_warm_manager(self.config)
        warm.apply_keep_alive(payload)

        local_cfg = self.config.get("local_llm", {}) if isinstance(self.config, dict) else {}
        local_timeout = float(local_cfg.get("timeout_seconds", 45) or 45)
        monitor = getattr(self, "llm_performance", None) or get_llm_performance(self.config)
        context = monitor.context_metrics(messages, tools)
        gpu_before = monitor.sample_gpu()
        started = time.perf_counter()
        warm.begin_inference()
        try:
            try:
                response = requests.post(
                    self.ollama_host + "/api/chat",
                    json=payload,
                    timeout=float(timeout if timeout is not None else local_timeout),
                    headers={"User-Agent": "Nova-Agent/0.9.5"},
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("Ollama devolvió una respuesta no estructurada.")
            except Exception as exc:
                wall_ms = (time.perf_counter() - started) * 1000.0
                gpu_after = monitor.sample_gpu()
                self._last_llm_metrics = monitor.record_failure(
                    model=self.model,
                    label=performance_label,
                    wall_ms=wall_ms,
                    context=context,
                    gpu_before=gpu_before,
                    gpu_after=gpu_after,
                    error_type=type(exc).__name__,
                )
                raise

            wall_ms = (time.perf_counter() - started) * 1000.0
            gpu_after = monitor.sample_gpu()
            self._last_llm_metrics = monitor.record_success(
                model=self.model,
                label=performance_label,
                wall_ms=wall_ms,
                response=data,
                context=context,
                gpu_before=gpu_before,
                gpu_after=gpu_after,
            )
            return data
        finally:
            warm.end_inference()

    def ask(self, user_text):
        action = warm_direct_intent(user_text)
        if action:
            manager = getattr(self, "llm_warm", None) or get_llm_warm_manager(self.config)
            if action == "preload":
                report = manager.preload(reason="user")
                if report.get("loaded"):
                    return "Qwen quedó precargado.\n\n" + _format_warm_status(report)
                if report.get("preload_skipped_reason"):
                    return "La precarga está suspendida temporalmente.\n\n" + _format_warm_status(report)
                return "No pude precargar Qwen.\n\n" + _format_warm_status(report)
            if action == "unload":
                report = manager.unload(reason="user")
                if report.get("unload_deferred"):
                    return "Esperaré a que termine la inferencia actual antes de liberar el modelo.\n\n" + _format_warm_status(report)
                if not report.get("loaded"):
                    return "Liberé el modelo local de la memoria de Ollama.\n\n" + _format_warm_status(report)
                return "Intenté liberar el modelo, pero Ollama todavía lo reporta cargado.\n\n" + _format_warm_status(report)
            return _format_warm_status(manager.status(refresh=True))
        return original_ask(self, user_text)

    def system_prompt(self):
        base = original_prompt(self) if callable(original_prompt) else ""
        return base + """

INSTANT WAKE / LLM WARM MANAGER
- Nova puede precargar el modelo local con una petición vacía de Ollama; esa precarga no contiene un prompt de usuario.
- No confundas "modelo cargado" con "GPU libre": informa la VRAM observada cuando esté disponible.
- Si el usuario pide liberar VRAM, el Warm Manager puede descargar explícitamente el modelo de Ollama.
- Una política temporal como Gaming Awareness puede reducir keep_alive o suspender precarga; nunca interrumpas una inferencia activa para descargar el modelo.
"""

    Agent.__init__ = init
    Agent._ollama_chat = ollama_chat
    Agent._chat = ollama_chat
    Agent._call_model = ollama_chat
    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_instant_wake_patched = True
    return mod
