from __future__ import annotations

"""Filtros de falsos positivos para Gaming Awareness.

Una ruta de Steam/Xbox/Epic no basta para afirmar que un proceso sea un juego.
"""

from typing import Any
import psutil
from . import gaming_awareness as mod

DEFAULT_IGNORED_PROCESSES = [
    "wallpaper32.exe", "wallpaper64.exe", "wallpaper_engine.exe",
    "steam.exe", "steamwebhelper.exe", "gameoverlayui.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe", "minecraftlauncher.exe",
    "riotclientservices.exe", "riotclientux.exe", "riotclientuxrender.exe",
    "battle.net.exe", "agent.exe", "eadesktop.exe", "ea.exe",
    "upc.exe", "ubisoftconnect.exe", "rockstar-games-launcher.exe",
    "socialclubhelper.exe", "galaxyclient.exe", "crashpad_handler.exe",
    "unitycrashhandler32.exe", "unitycrashhandler64.exe", "werfault.exe",
    "cefsharp.browsersubprocess.exe",
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
    ignored_paths = [_norm(x) for x in self.gaming_config.get("ignored_game_path_markers", []) if str(x).strip()]
    mc_markers = [str(x).casefold() for x in self.gaming_config.get("minecraft_command_markers", []) if str(x).strip()]
    try:
        rows = psutil.process_iter(["pid", "name", "exe", "cmdline", "create_time"])
    except Exception:
        return None

    for proc in rows:
        try:
            info = proc.info
            name = str(info.get("name") or "")
            norm_name = _norm(name)
            exe = str(info.get("exe") or "")
            norm_exe = _norm(exe)
            cmdline = " ".join(str(x) for x in (info.get("cmdline") or [])).casefold()
            created = float(info.get("create_time") or 0)
            if norm_name in configured:
                return {"pid": int(info.get("pid") or 0), "process": name, "exe": exe,
                        "create_time": created, "source": "process",
                        "reason": "proceso configurado como juego", "foreground": False}
            if norm_name in ignored or (norm_exe and any(m and m in norm_exe for m in ignored_paths)):
                continue
            if norm_name == "javaw.exe" and any(marker in cmdline for marker in mc_markers):
                return {"pid": int(info.get("pid") or 0), "process": name, "exe": exe,
                        "create_time": created, "source": "minecraft_java",
                        "reason": "javaw con argumentos de Minecraft/Forge/Fabric", "foreground": False}
            # No hay fallback por ruta de biblioteca: ruta != evidencia de juego.
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return None


def install_gaming_detection_filters():
    Manager = mod.GamingAwarenessManager
    if getattr(Manager, "_nova_gaming_detection_filters_patched", False):
        return Manager
    mod.DEFAULT_GAMING_CONFIG.setdefault("ignored_game_processes", list(DEFAULT_IGNORED_PROCESSES))
    mod.DEFAULT_GAMING_CONFIG.setdefault("ignored_game_path_markers", list(DEFAULT_IGNORED_PATH_MARKERS))
    mod.DEFAULT_GAMING_CONFIG.setdefault("perception_max_age_seconds", 6.0)
    Manager._scan_processes = _scan_processes_filtered
    Manager._nova_gaming_detection_filters_patched = True
    return Manager
