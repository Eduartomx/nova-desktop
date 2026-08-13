from __future__ import annotations

"""Rutas deterministas de latencia mínima para Nova.

Estas rutas se ejecutan antes del pipeline generativo completo. No consultan
Semantic Memory, Ollama ni Expert Escalation para datos locales que Nova puede
leer directamente de su propio estado.
"""

import re
import unicodedata
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9ñü\s]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def fast_direct_intent(text: str) -> str | None:
    t = _normalize(text)
    if not t:
        return None

    resident_exact = {
        "nova estas ejecutandote en segundo plano": "resident_status",
        "estas ejecutandote en segundo plano": "resident_status",
        "nova estas en segundo plano": "resident_status",
        "estas en segundo plano": "resident_status",
        "nova ocultate en la bandeja": "resident_hide",
        "ocultate en la bandeja": "resident_hide",
        "nova muestrate": "resident_show",
        "muestrate": "resident_show",
        "muestra nova": "resident_show",
        "nova no inicies con windows": "resident_autostart_off",
        "no inicies con windows": "resident_autostart_off",
        "desactiva inicio con windows": "resident_autostart_off",
        "nova inicia con windows": "resident_autostart_on",
        "inicia con windows": "resident_autostart_on",
        "activa inicio con windows": "resident_autostart_on",
    }
    if t in resident_exact:
        return resident_exact[t]

    # Evita interceptar preguntas sobre versiones de Python/Ollama/modelos/apps.
    foreign_version = any(x in t for x in ("python", "ollama", "qwen", "groq", "cerebras", "windows", "driver", "cuda"))
    if not foreign_version and (
        ("version" in t and "nova" in t)
        or t in {"que version tienes", "cual es tu version", "dime tu version", "tu version"}
    ):
        return "version"

    if t in {
        "estado del sistema", "estado del pc", "estado de mi pc", "recursos del sistema",
        "como esta mi pc", "como esta el pc", "uso de cpu", "uso de ram", "uso de gpu",
        "cpu ram gpu", "estado de recursos",
    }:
        return "system_status"

    if t in {
        "procesos que mas consumen", "que procesos consumen mas", "procesos con mas consumo",
        "procesos mas pesados", "procesos pesados", "top procesos", "procesos del sistema",
    }:
        return "processes"

    if t in {
        "workspace activo", "cual es el workspace activo", "proyecto activo",
        "cual es el proyecto activo", "en que proyecto estamos", "que proyecto esta activo",
    }:
        return "workspace"

    return None


def _read_version() -> str:
    for path in (PROJECT_ROOT / "NOVA_VERSION.txt", PROJECT_ROOT.parent / "VERSION"):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            continue
    try:
        from . import __version__
        return str(__version__ or "desconocida")
    except Exception:
        return "desconocida"


def _format_system_status(row: dict[str, Any]) -> str:
    if not isinstance(row, dict) or not row.get("ok"):
        return "No pude leer el estado local del sistema."
    parts = [
        f"CPU {float(row.get('cpu_percent') or 0):.0f}%",
        f"RAM {float(row.get('memory_percent') or 0):.0f}% ({float(row.get('memory_used_gb') or 0):.1f}/{float(row.get('memory_total_gb') or 0):.1f} GB)",
    ]
    gpu = row.get("gpu") if isinstance(row.get("gpu"), dict) else None
    if gpu:
        parts.append(
            f"GPU {gpu.get('name') or 'NVIDIA'} {float(gpu.get('utilization_percent') or 0):.0f}% · "
            f"VRAM {float(gpu.get('memory_used_mb') or 0):.0f}/{float(gpu.get('memory_total_mb') or 0):.0f} MB"
        )
    return "Estado local: " + " · ".join(parts) + "."


def _format_processes(row: dict[str, Any]) -> str:
    rows = list((row or {}).get("processes") or [])[:8]
    if not rows:
        return "No pude obtener la lista de procesos."
    lines = ["Procesos con mayor consumo observado:"]
    for item in rows:
        lines.append(
            f"- {item.get('name') or '?'} (PID {item.get('pid') or '?'}) · "
            f"CPU {float(item.get('cpu_percent') or 0):.1f}% · RAM {float(item.get('memory_percent') or 0):.1f}%"
        )
    return "\n".join(lines)


def _format_workspace(agent) -> str:
    try:
        row = agent.memory.active_workspace()
    except Exception:
        row = None
    if not row:
        return "No hay un workspace/proyecto activo en Nova."
    return (
        f"Workspace activo: {row.get('name') or '?'} · tipo {row.get('kind') or 'generic'} · "
        f"{row.get('path') or 'sin ruta'}."
    )


def _resident_route(agent, action: str) -> str:
    from .runtime_lifecycle import get_current_lifecycle

    lifecycle = get_current_lifecycle()
    if lifecycle is None:
        return "Resident Mode todavía no está disponible en esta sesión."

    if action == "resident_status":
        status = lifecycle.status()
        tray = status.get("tray") or {}
        visible = "oculta en la bandeja" if status.get("window_hidden") else "con la ventana visible"
        tray_text = "bandeja activa" if tray.get("available") else "bandeja no disponible"
        return f"Estado residente: {status.get('state')} · {visible} · {tray_text}."
    if action == "resident_hide":
        return "Seguiré activa en segundo plano." if lifecycle.hide_window() else "No me ocultaré porque la bandeja no está disponible."
    if action == "resident_show":
        lifecycle.show_window()
        return "Ventana de Nova restaurada."

    manager = getattr(lifecycle, "autostart", None)
    if manager is None:
        return "El inicio con Windows no está disponible en esta sesión."
    enabled = action == "resident_autostart_on"
    if not manager.set_enabled(enabled):
        return "No pude cambiar el inicio con Windows."
    agent.config.setdefault("resident_mode", {})["start_with_windows"] = enabled
    try:
        from .config import save_config
        save_config(agent.config)
    except Exception:
        return "El inicio con Windows cambió en el sistema, pero no pude actualizar config.json."
    return "Inicio con Windows activado." if enabled else "Inicio con Windows desactivado."


def install_agent_fast_routing():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_fast_routing_patched", False):
        return mod

    original_ask = Agent.ask

    def ask(self, user_text):
        text = str(user_text or "").strip()
        action = fast_direct_intent(text)
        if action is None:
            return original_ask(self, user_text)

        self._last_fast_route = action
        if action.startswith("resident_"):
            result = _resident_route(self, action)
            self._last_tool_trace = [{"name": action, "ok": not result.startswith("No pude")}]
        elif action == "version":
            result = f"Soy Nova v{_read_version()}."
            self._last_tool_trace = [{"name": "nova_version", "ok": True}]
        elif action == "system_status":
            status = self.tools.system_status()
            result = _format_system_status(status)
            self._last_tool_trace = [{"name": "system_status", "ok": bool(status.get('ok')) if isinstance(status, dict) else False}]
        elif action == "processes":
            status = self.tools.list_processes(8)
            result = _format_processes(status)
            self._last_tool_trace = [{"name": "list_processes", "ok": bool(status.get('ok')) if isinstance(status, dict) else False}]
        else:
            result = _format_workspace(self)
            self._last_tool_trace = [{"name": "active_workspace", "ok": True}]

        try:
            self.memory.add_message("user", text)
            self.memory.add_message("assistant", result)
        except Exception:
            pass
        self._last_response = result
        return result

    Agent.ask = ask
    Agent._nova_fast_routing_patched = True
    return mod
