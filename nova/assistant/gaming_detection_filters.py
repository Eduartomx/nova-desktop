from __future__ import annotations

"""Filtros de falsos positivos para Gaming Awareness.

Las bibliotecas de Steam/Xbox/Epic también contienen utilidades que no son
juegos. 0.9.5 trataba cualquier ejecutable bajo esas rutas como juego, por lo
que aplicaciones como Wallpaper Engine podían activar Gaming Mode de forma
permanente. Este adaptador añade exclusiones configurables antes de aplicar la
heurística de ruta.
"""

from typing import Any

import psutil

from . import gaming_awareness as mod


DEFAULT_IGNORED_PROCESSES = [
    "wallpaper32.exe",
    "wallpaper64.exe",
    "wallpaper_engine.exe",
]

DEFAULT_IGNORED_PATH_MARKERS = [
    "\\steamapps\\common\\wallpaper_engine\\",
    "/steamapps/common/wallpaper_engine/",
]


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold().replace("/", "\\")


def _scan_processes_filtered(self) -> dict[str, Any] | None:
    if self._process_sensor is not None:
        try:
            found = self._process_sensor()
            return dict(found) if isinstance(found, dict) else None
        except Exception:
            return None

    configured = {_norm(x) for x in self.gaming_config.get("game_processes", []) if str(x).strip()}
    ignored = {_norm(x) for x in self.gaming_config.get("ignored_game_processes", []) if str(x).strip()}
    path_markers = [_norm(x) for x in self.gaming_config.get("game_path_markers", []) if str(x).strip()]
    ignored_paths = [_norm(x) for x in self.gaming_config.get("ignored_game_path_markers", []) if str(x).strip()]
    mc_markers = [str(x).casefold() for x in self.gaming_config.get("minecraft_command_markers", []) if str(x).strip()]

    try:
        rows = psutil.process_iter(["pid", "name", "exe", "cmdline"])
    except Exception:
        return None

    for proc in rows:
        try:
            info = proc.info
            name = str(info.get("name") or "")
            norm_name = _norm(name)
            exe = _norm(info.get("exe") or "")
            cmdline = " ".join(str(x) for x in (info.get("cmdline") or [])).casefold()

            # Una inclusión explícita del usuario gana sobre las exclusiones.
            if norm_name in configured:
                return {
                    "pid": int(info.get("pid") or 0),
                    "process": name,
                    "source": "process",
                    "reason": "proceso configurado como juego",
                    "foreground": False,
                }

            # Utilidades instaladas dentro de bibliotecas de juegos no deben
            # activar Gaming Mode. Wallpaper Engine es el primer caso validado.
            if norm_name in ignored:
                continue
            if exe and any(marker and marker in exe for marker in ignored_paths):
                continue

            if exe and any(marker and marker in exe for marker in path_markers):
                return {
                    "pid": int(info.get("pid") or 0),
                    "process": name,
                    "source": "game_path",
                    "reason": "ejecutable dentro de una biblioteca de juegos conocida",
                    "foreground": False,
                }

            if norm_name == "javaw.exe" and any(marker in cmdline for marker in mc_markers):
                return {
                    "pid": int(info.get("pid") or 0),
                    "process": name,
                    "source": "minecraft_java",
                    "reason": "javaw con argumentos de Minecraft/Forge/Fabric",
                    "foreground": False,
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return None


def install_gaming_detection_filters():
    Manager = mod.GamingAwarenessManager
    if getattr(Manager, "_nova_gaming_detection_filters_patched", False):
        return Manager

    # Extender los defaults antes de que config_gaming haga su deepcopy permite
    # migrar config.json automáticamente sin pisar preferencias existentes.
    mod.DEFAULT_GAMING_CONFIG.setdefault("ignored_game_processes", list(DEFAULT_IGNORED_PROCESSES))
    mod.DEFAULT_GAMING_CONFIG.setdefault("ignored_game_path_markers", list(DEFAULT_IGNORED_PATH_MARKERS))

    Manager._scan_processes = _scan_processes_filtered
    Manager._nova_gaming_detection_filters_patched = True
    return Manager
