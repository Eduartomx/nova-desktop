from __future__ import annotations

"""Resiliencia de proveedores para Expert Escalation.

v0.8.2.1 cambia el orden predeterminado a Groq -> Cerebras y añade un circuit
breaker persistente para evitar repetir proveedores que acaban de responder con
errores de autenticación, pago, rate-limit o servidor.

No se persisten prompts, respuestas ni API keys. La tabla de health contiene solo
proveedor, código HTTP, motivo técnico y tiempo de reapertura.
"""

import json
import time
from typing import Any


_NEW_ORDER = ["groq", "cerebras"]
_OLD_ORDER = ["cerebras", "groq"]
_OLD_GROQ_MODEL = "qwen/qwen3.6-27b"
_NEW_GROQ_MODEL = "openai/gpt-oss-120b"

DEFAULT_CIRCUIT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "auth_cooldown_seconds": 6 * 60 * 60,
    "payment_cooldown_seconds": 12 * 60 * 60,
    "rate_limit_cooldown_seconds": 90,
    "server_cooldown_seconds": 120,
}


def _clone(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        return value


def normalize_expert_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Migra únicamente defaults conocidos; respeta personalizaciones reales."""
    cfg = _clone(config) if isinstance(config, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}

    order = list(cfg.get("provider_order") or [])
    if not order or order == _OLD_ORDER:
        cfg["provider_order"] = list(_NEW_ORDER)

    free_api = cfg.setdefault("free_api", {})
    if not isinstance(free_api, dict):
        free_api = {}
        cfg["free_api"] = free_api
    groq = free_api.setdefault("groq", {})
    if not isinstance(groq, dict):
        groq = {}
        free_api["groq"] = groq
    if not groq.get("model") or str(groq.get("model")) == _OLD_GROQ_MODEL:
        groq["model"] = _NEW_GROQ_MODEL

    circuit = cfg.setdefault("circuit_breaker", {})
    if not isinstance(circuit, dict):
        circuit = {}
        cfg["circuit_breaker"] = circuit
    for key, value in DEFAULT_CIRCUIT_CONFIG.items():
        circuit.setdefault(key, value)
    return cfg


class ProviderHTTPError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = int(status_code)
        super().__init__(f"http_{self.status_code}")


def install_expert_resilience():
    from . import config as config_mod
    from . import expert_escalation as mod

    if getattr(mod.ExpertEscalation, "_nova_resilience_patched", False):
        return mod

    # Actualiza defaults del dominio antes de que load_config() sea llamado por app.py.
    mod.DEFAULT_EXPERT_CONFIG["provider_order"] = list(_NEW_ORDER)
    mod.DEFAULT_EXPERT_CONFIG.setdefault("free_api", {}).setdefault("groq", {})["model"] = _NEW_GROQ_MODEL
    mod.DEFAULT_EXPERT_CONFIG["circuit_breaker"] = dict(DEFAULT_CIRCUIT_CONFIG)

    default_expert = config_mod.DEFAULT_CONFIG.setdefault("expert_escalation", {})
    default_expert["provider_order"] = list(_NEW_ORDER)
    default_expert.setdefault("free_api", {}).setdefault("groq", {})["model"] = _NEW_GROQ_MODEL
    default_expert["circuit_breaker"] = dict(DEFAULT_CIRCUIT_CONFIG)

    # Migra el config local solo cuando conserva exactamente los defaults de 0.8.2.
    original_load_config = config_mod.load_config
    if not getattr(original_load_config, "_nova_expert_resilience", False):
        def load_config():
            cfg = original_load_config()
            before = _clone(cfg.get("expert_escalation", {}))
            after = normalize_expert_config(before)
            if before != after:
                cfg["expert_escalation"] = after
                try:
                    config_mod.save_config(cfg)
                except Exception:
                    pass
            return cfg

        load_config._nova_expert_resilience = True
        config_mod.load_config = load_config

    original_init = mod.ExpertEscalation.__init__
    original_configure = mod.ExpertEscalation.configure
    original_safe_error = mod._safe_error_code
    original_call_provider = mod.ExpertEscalation._call_provider
    original_provider_status = mod.ExpertEscalation.provider_status

    def _init_health_table(self):
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS expert_provider_health (
                    provider TEXT PRIMARY KEY,
                    open_until REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    http_status INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )"""
            )
            conn.commit()

    def init(self, config=None, memory=None, db_path=None):
        original_init(self, normalize_expert_config(config), memory=memory, db_path=db_path)
        _init_health_table(self)

    def configure(self, config=None):
        return original_configure(self, normalize_expert_config(config))

    def circuit_config(self) -> dict[str, Any]:
        cfg = self.config.get("circuit_breaker", {}) if isinstance(self.config, dict) else {}
        out = dict(DEFAULT_CIRCUIT_CONFIG)
        if isinstance(cfg, dict):
            out.update(cfg)
        return out

    def provider_health(self, provider: str) -> dict[str, Any]:
        provider = str(provider or "").casefold().strip()
        if not provider:
            return {"open": False, "provider": ""}
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT provider,open_until,reason,http_status,updated_at FROM expert_provider_health WHERE provider=?",
                (provider,),
            ).fetchone()
            if row is None:
                return {
                    "provider": provider,
                    "open": False,
                    "open_until": 0.0,
                    "retry_after_seconds": 0,
                    "reason": "",
                    "http_status": 0,
                }
            data = dict(row)
            open_until = float(data.get("open_until", 0.0) or 0.0)
            if open_until and open_until <= now:
                conn.execute(
                    "UPDATE expert_provider_health SET open_until=0,reason='',http_status=0,updated_at=? WHERE provider=?",
                    (now, provider),
                )
                conn.commit()
                open_until = 0.0
                data["reason"] = ""
                data["http_status"] = 0
            data["provider"] = provider
            data["open_until"] = open_until
            data["open"] = bool(open_until > now)
            data["retry_after_seconds"] = max(0, int(open_until - now)) if data["open"] else 0
            return data

    def close_provider_circuit(self, provider: str) -> None:
        provider = str(provider or "").casefold().strip()
        if not provider:
            return
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO expert_provider_health(provider,open_until,reason,http_status,updated_at)
                   VALUES(?,0,'',0,?)
                   ON CONFLICT(provider) DO UPDATE SET open_until=0,reason='',http_status=0,updated_at=excluded.updated_at""",
                (provider, time.time()),
            )
            conn.commit()

    def open_provider_circuit(self, provider: str, status_code: int) -> dict[str, Any]:
        status = int(status_code or 0)
        cfg = circuit_config(self)
        if not cfg.get("enabled", True):
            return provider_health(self, provider)
        if status in {401, 403}:
            seconds = int(cfg.get("auth_cooldown_seconds", 21600))
            reason = "auth"
        elif status == 402:
            seconds = int(cfg.get("payment_cooldown_seconds", 43200))
            reason = "payment_required"
        elif status == 429:
            seconds = int(cfg.get("rate_limit_cooldown_seconds", 90))
            reason = "rate_limited"
        elif status >= 500:
            seconds = int(cfg.get("server_cooldown_seconds", 120))
            reason = "server_error"
        else:
            return provider_health(self, provider)
        until = time.time() + max(1, seconds)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO expert_provider_health(provider,open_until,reason,http_status,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET
                     open_until=excluded.open_until,
                     reason=excluded.reason,
                     http_status=excluded.http_status,
                     updated_at=excluded.updated_at""",
                (str(provider), until, reason, status, time.time()),
            )
            conn.commit()
        return provider_health(self, provider)

    @staticmethod
    def post_json(endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        import requests

        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
        if int(response.status_code) >= 400:
            raise ProviderHTTPError(int(response.status_code))
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("invalid_json_response")
        return data

    def safe_error_code(exc: BaseException) -> str:
        if isinstance(exc, ProviderHTTPError):
            return f"http_{exc.status_code}"
        return original_safe_error(exc)

    def call_provider(self, provider: str, packet: dict[str, Any]) -> dict[str, Any]:
        provider = str(provider or "").casefold().strip()
        health = provider_health(self, provider)
        if health.get("open"):
            return {
                "ok": False,
                "provider": provider,
                "model": str(self._provider_config(provider).get("model") or ""),
                "error": "provider_circuit_open",
                "http_status": int(health.get("http_status", 0) or 0),
                "circuit_reason": str(health.get("reason") or ""),
                "retry_after_seconds": int(health.get("retry_after_seconds", 0) or 0),
                "duration_ms": 0.0,
            }

        result = original_call_provider(self, provider, packet)
        if result.get("ok"):
            close_provider_circuit(self, provider)
            return result

        error = str(result.get("error") or "")
        if error.startswith("http_"):
            try:
                status = int(error.split("_", 1)[1])
            except Exception:
                status = 0
            if status:
                health = open_provider_circuit(self, provider, status)
                result["http_status"] = status
                if health.get("open"):
                    result["circuit_opened"] = True
                    result["circuit_reason"] = str(health.get("reason") or "")
                    result["retry_after_seconds"] = int(health.get("retry_after_seconds", 0) or 0)
        return result

    def provider_status(self):
        status = original_provider_status(self)
        providers = status.get("providers") or {}
        for name, row in providers.items():
            health = provider_health(self, name)
            row["circuit_open"] = bool(health.get("open"))
            row["circuit_reason"] = str(health.get("reason") or "")
            row["retry_after_seconds"] = int(health.get("retry_after_seconds", 0) or 0)
            row["last_http_status"] = int(health.get("http_status", 0) or 0)
        status["provider_order"] = list(self.config.get("provider_order") or _NEW_ORDER)
        status["circuit_breaker"] = {
            "enabled": bool(circuit_config(self).get("enabled", True)),
            "persistent": True,
        }
        return status

    mod.ExpertEscalation.__init__ = init
    mod.ExpertEscalation.configure = configure
    mod.ExpertEscalation._init_provider_health = _init_health_table
    mod.ExpertEscalation._circuit_config = circuit_config
    mod.ExpertEscalation.provider_health = provider_health
    mod.ExpertEscalation.open_provider_circuit = open_provider_circuit
    mod.ExpertEscalation.close_provider_circuit = close_provider_circuit
    mod.ExpertEscalation._post_json = staticmethod(post_json)
    mod.ExpertEscalation._call_provider = call_provider
    mod.ExpertEscalation.provider_status = provider_status
    mod._safe_error_code = safe_error_code
    mod.ExpertEscalation._nova_resilience_patched = True
    return mod
