from __future__ import annotations

"""Gaming Awareness para Nova.

Detecta juegos con señales locales baratas (Perception Engine + procesos), aplica
una política de VRAM al Warm Manager y reduce la frecuencia de Perception
mientras el juego está activo. No inspecciona memoria de otros procesos, no
captura pantalla y no inyecta nada dentro del juego.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import psutil

from .llm_performance import get_llm_performance
from .llm_warm import get_llm_warm_manager
from .perception import get_perception


DEFAULT_GAMING_CONFIG: dict[str, Any] = {
    "enabled": True,
    "auto_detect": True,
    "poll_seconds": 2.0,
    "enter_dwell_seconds": 2.5,
    "exit_dwell_seconds": 7.0,
    "release_policy": "smart",
    "auto_release_llm": True,
    "keep_llm_loaded_during_game": False,
    "llm_keep_alive_during_game": 0,
    "vram_release_percent": 65.0,
    "vram_min_free_mb": 2600.0,
    "restore_preload_after_game": True,
    "restore_delay_seconds": 4.0,
    "perception_poll_ms_during_game": 2500,
    "game_processes": [
        "fortniteclient-win64-shipping.exe",
        "robloxplayerbeta.exe",
        "vrchat.exe",
        "minecraft.windows.exe",
    ],
    "game_path_markers": [
        "\\steamapps\\common\\",
        "/steamapps/common/",
        "\\xboxgames\\",
        "/xboxgames/",
        "\\epic games\\",
        "/epic games/",
    ],
    "minecraft_command_markers": [
        "minecraft", "net.minecraft", "forge", "fabric-loader", "lwjgl",
    ],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold().replace("/", "\\")


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


class GamingAwarenessManager:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        warm_manager=None,
        perception=None,
        gpu_sensor: Callable[[], dict[str, Any] | None] | None = None,
        process_sensor: Callable[[], dict[str, Any] | None] | None = None,
    ):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restore_timer: threading.Timer | None = None
        self._manual_mode = "auto"
        self._active = False
        self._game: dict[str, Any] = {}
        self._candidate_since = 0.0
        self._clear_since = 0.0
        self._active_since = ""
        self._last_action = ""
        self._last_action_at = ""
        self._release_reason = ""
        self._llm_released = False
        self._last_gpu: dict[str, Any] = {}
        self._last_vram_reclaimed_mb = 0.0
        self._warm = warm_manager
        self._perception = perception
        self._gpu_sensor = gpu_sensor
        self._process_sensor = process_sensor
        self.update_config(config or {})

    def update_config(self, config: dict[str, Any] | None = None):
        config = config or {}
        cfg = config.get("gaming_awareness", {}) if isinstance(config, dict) else {}
        merged = dict(DEFAULT_GAMING_CONFIG)
        if isinstance(cfg, dict):
            merged.update(cfg)
        with self._lock:
            self.config = config
            self.gaming_config = merged
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.gaming_config.get("enabled", True))

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _warm_manager(self):
        if self._warm is None:
            self._warm = get_llm_warm_manager(self.config)
        return self._warm

    def _perception_engine(self):
        if self._perception is None:
            self._perception = get_perception(self.config)
        return self._perception

    def _gpu_snapshot(self) -> dict[str, Any]:
        try:
            raw = self._gpu_sensor() if self._gpu_sensor is not None else get_llm_performance(self.config).sample_gpu()
        except Exception:
            raw = None
        raw = dict(raw or {})
        used = _safe_float(raw.get("vram_used_mb"))
        total = _safe_float(raw.get("vram_total_mb"))
        raw["vram_free_mb"] = round(max(0.0, total - used), 1) if total else 0.0
        raw["vram_percent"] = round(used * 100.0 / total, 1) if total else 0.0
        with self._lock:
            self._last_gpu = raw
        return raw

    def _foreground_game(self) -> dict[str, Any] | None:
        try:
            engine = self._perception_engine()
            state = engine.current(refresh=False)
            ext = state.get("external") if isinstance(state, dict) else None
            if isinstance(ext, dict) and str(ext.get("app_kind") or "") == "game":
                return {
                    "pid": int(ext.get("pid") or 0),
                    "process": str(ext.get("process") or ""),
                    "title": str(ext.get("title") or "")[:180],
                    "source": "foreground",
                    "reason": "Perception Engine clasificó la ventana activa como juego",
                    "foreground": True,
                }
        except Exception:
            pass
        return None

    def _scan_processes(self) -> dict[str, Any] | None:
        if self._process_sensor is not None:
            try:
                found = self._process_sensor()
                return dict(found) if isinstance(found, dict) else None
            except Exception:
                return None

        configured = {_norm(x) for x in self.gaming_config.get("game_processes", []) if str(x).strip()}
        path_markers = [_norm(x) for x in self.gaming_config.get("game_path_markers", []) if str(x).strip()]
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
                if norm_name in configured:
                    return {
                        "pid": int(info.get("pid") or 0),
                        "process": name,
                        "source": "process",
                        "reason": "proceso configurado como juego",
                        "foreground": False,
                    }
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

    def detect_game(self) -> dict[str, Any] | None:
        foreground = self._foreground_game()
        if foreground:
            return foreground
        return self._scan_processes()

    def _should_release(self, game: dict[str, Any], gpu: dict[str, Any], warm: dict[str, Any]) -> tuple[bool, str]:
        if not bool(self.gaming_config.get("auto_release_llm", True)):
            return False, "liberación automática desactivada"
        if bool(self.gaming_config.get("keep_llm_loaded_during_game", False)):
            return False, "el usuario pidió mantener Qwen cargado durante juegos"
        policy = str(self.gaming_config.get("release_policy", "smart") or "smart").casefold()
        if policy == "never":
            return False, "política never"
        if policy == "always":
            return True, "política always"
        if bool(game.get("foreground")):
            return True, "juego en primer plano"
        used_percent = _safe_float(gpu.get("vram_percent"))
        free_mb = _safe_float(gpu.get("vram_free_mb"))
        if used_percent and used_percent >= _safe_float(self.gaming_config.get("vram_release_percent", 65.0)):
            return True, f"VRAM en {used_percent:.1f}%"
        if free_mb and free_mb <= _safe_float(self.gaming_config.get("vram_min_free_mb", 2600.0)):
            return True, f"solo {free_mb:.0f} MB de VRAM libres"
        if not gpu and _safe_float(warm.get("size_vram_mb")) >= 2000:
            return True, "sin telemetría GPU y el modelo reserva al menos 2 GB"
        return False, "sin presión de VRAM suficiente"

    def _apply_perception_throttle(self, active: bool):
        try:
            engine = self._perception_engine()
            if not hasattr(engine, "set_runtime_poll_interval_ms"):
                return
            if active:
                engine.set_runtime_poll_interval_ms(int(self.gaming_config.get("perception_poll_ms_during_game", 2500) or 2500))
            else:
                engine.set_runtime_poll_interval_ms(None)
        except Exception:
            pass

    def _apply_active_policy(self):
        if not self._active:
            return
        warm = self._warm_manager()
        keep_loaded = bool(self.gaming_config.get("keep_llm_loaded_during_game", False))
        if keep_loaded:
            warm.clear_preload_suppression("gaming_mode")
            warm.clear_runtime_keep_alive_override("gaming_mode")
            self._apply_perception_throttle(True)
            return

        warm.suppress_preload("gaming_mode")
        warm.set_runtime_keep_alive_override(
            self.gaming_config.get("llm_keep_alive_during_game", 0),
            reason="gaming_mode",
        )
        self._apply_perception_throttle(True)

        warm_status = warm.status(refresh=True)
        if not warm_status.get("loaded"):
            return
        if warm_status.get("warming") or int(warm_status.get("active_inferences") or 0) > 0:
            with self._lock:
                self._last_action = "esperando que termine la inferencia antes de liberar Qwen"
                self._last_action_at = _utc_now()
            return

        gpu_before = self._gpu_snapshot()
        release, reason = self._should_release(dict(self._game), gpu_before, warm_status)
        with self._lock:
            self._release_reason = reason
        if not release:
            return

        result = warm.unload(timeout=4.0, reason="gaming_mode")
        if result.get("loaded") is False:
            time.sleep(0.08)
            gpu_after = self._gpu_snapshot()
            before = _safe_float(gpu_before.get("vram_used_mb"))
            after = _safe_float(gpu_after.get("vram_used_mb"))
            reclaimed = max(0.0, before - after) if before and after else _safe_float(warm_status.get("size_vram_mb"))
            with self._lock:
                self._llm_released = True
                self._last_vram_reclaimed_mb = round(reclaimed, 1)
                self._last_action = "Qwen liberado para priorizar el juego"
                self._last_action_at = _utc_now()

    def _enter(self, game: dict[str, Any]):
        with self._lock:
            if self._active:
                self._game = dict(game)
                return
            self._active = True
            self._game = dict(game)
            self._active_since = _utc_now()
            self._clear_since = 0.0
            self._last_action = "Gaming Mode activado"
            self._last_action_at = _utc_now()
            self._release_reason = ""
            self._llm_released = False
            self._last_vram_reclaimed_mb = 0.0
        self._apply_active_policy()

    def _schedule_restore(self):
        if not bool(self.gaming_config.get("restore_preload_after_game", True)):
            return
        if not self._llm_released:
            return
        warm = self._warm_manager()
        cached = warm.cached_status()
        if str(cached.get("last_unload_reason") or "") != "gaming_mode":
            return
        delay = max(0.0, float(self.gaming_config.get("restore_delay_seconds", 4.0) or 0.0))

        def restore():
            if self._active or self._manual_mode == "on":
                return
            warm.clear_preload_suppression("gaming_mode")
            warm.clear_runtime_keep_alive_override("gaming_mode")
            if warm.preload_on_start:
                report = warm.preload(reason="gaming_restore")
                with self._lock:
                    if report.get("loaded"):
                        self._last_action = "Qwen precargado de nuevo al salir del juego"
                    else:
                        self._last_action = "Qwen quedó bajo demanda al salir del juego"
                    self._last_action_at = _utc_now()

        try:
            if self._restore_timer is not None:
                self._restore_timer.cancel()
        except Exception:
            pass
        self._restore_timer = threading.Timer(delay, restore)
        self._restore_timer.daemon = True
        self._restore_timer.start()

    def _exit(self, reason: str = "juego cerrado"):
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._game = {}
            self._candidate_since = 0.0
            self._clear_since = 0.0
            self._active_since = ""
            self._last_action = "Gaming Mode desactivado · " + str(reason)
            self._last_action_at = _utc_now()
        self._apply_perception_throttle(False)
        warm = self._warm_manager()
        warm.clear_preload_suppression("gaming_mode")
        warm.clear_runtime_keep_alive_override("gaming_mode")
        self._schedule_restore()

    def set_mode(self, mode: str) -> dict[str, Any]:
        mode = str(mode or "auto").casefold().strip()
        if mode not in {"auto", "on", "off"}:
            raise ValueError("Modo inválido; usa auto, on u off.")
        with self._lock:
            self._manual_mode = mode
            self._candidate_since = 0.0
            self._clear_since = 0.0
        if mode == "on":
            self._enter({
                "pid": 0,
                "process": "manual",
                "source": "manual",
                "reason": "Gaming Mode forzado por el usuario",
                "foreground": True,
            })
        elif mode == "off":
            self._exit("desactivado manualmente")
        return self.status(refresh=False)

    def set_keep_llm_loaded(self, enabled: bool) -> dict[str, Any]:
        value = bool(enabled)
        with self._lock:
            self.gaming_config["keep_llm_loaded_during_game"] = value
            if isinstance(self.config, dict):
                self.config.setdefault("gaming_awareness", {})["keep_llm_loaded_during_game"] = value
        if self._active:
            if value:
                warm = self._warm_manager()
                warm.clear_preload_suppression("gaming_mode")
                warm.clear_runtime_keep_alive_override("gaming_mode")
                if warm.cached_status().get("loaded") is False:
                    warm.start_background(reason="gaming_user_keep")
            else:
                self._apply_active_policy()
        return self.status(refresh=False)

    def tick(self) -> dict[str, Any]:
        if not self.enabled:
            if self._active:
                self._exit("Gaming Awareness desactivado")
            return self.status(refresh=False)

        mode = self._manual_mode
        now = time.monotonic()
        if mode == "on":
            if not self._active:
                self._enter({"process": "manual", "source": "manual", "reason": "Gaming Mode forzado", "foreground": True})
            else:
                self._apply_active_policy()
            return self.status(refresh=False)
        if mode == "off" or not bool(self.gaming_config.get("auto_detect", True)):
            if self._active:
                self._exit("detección automática desactivada")
            return self.status(refresh=False)

        game = self.detect_game()
        if game:
            self._clear_since = 0.0
            if self._active:
                with self._lock:
                    self._game = dict(game)
                self._apply_active_policy()
            else:
                if not self._candidate_since:
                    self._candidate_since = now
                dwell = max(0.0, float(self.gaming_config.get("enter_dwell_seconds", 2.5) or 0.0))
                if now - self._candidate_since >= dwell:
                    self._enter(game)
        else:
            self._candidate_since = 0.0
            if self._active:
                if not self._clear_since:
                    self._clear_since = now
                dwell = max(0.0, float(self.gaming_config.get("exit_dwell_seconds", 7.0) or 0.0))
                if now - self._clear_since >= dwell:
                    self._exit("ya no se detecta el juego")
        return self.status(refresh=False)

    def start(self):
        if not self.enabled:
            return self
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="nova-gaming-awareness", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        try:
            if self._restore_timer is not None:
                self._restore_timer.cancel()
        except Exception:
            pass
        thread = self._thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=max(0.0, float(timeout)))
        if self._active:
            self._exit("Nova se está cerrando")
        return self

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                pass
            elapsed = time.monotonic() - started
            interval = max(0.5, float(self.gaming_config.get("poll_seconds", 2.0) or 2.0))
            self._stop.wait(max(0.1, interval - elapsed))

    def status(self, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            try:
                self.tick()
            except Exception:
                pass
        with self._lock:
            game = dict(self._game)
            gpu = dict(self._last_gpu)
            report = {
                "ok": True,
                "enabled": self.enabled,
                "running": self.running,
                "mode": self._manual_mode,
                "auto_detect": bool(self.gaming_config.get("auto_detect", True)),
                "active": self._active,
                "active_since": self._active_since,
                "game": game,
                "release_policy": str(self.gaming_config.get("release_policy", "smart")),
                "keep_llm_loaded_during_game": bool(self.gaming_config.get("keep_llm_loaded_during_game", False)),
                "llm_released": self._llm_released,
                "release_reason": self._release_reason,
                "vram_reclaimed_mb": self._last_vram_reclaimed_mb,
                "last_action": self._last_action,
                "last_action_at": self._last_action_at,
                "gpu": gpu,
            }
        try:
            engine = self._perception_engine()
            if hasattr(engine, "effective_poll_interval_ms"):
                report["perception_poll_ms"] = engine.effective_poll_interval_ms()
        except Exception:
            pass
        return report

    @staticmethod
    def format_status(report: dict[str, Any]) -> str:
        if not report.get("enabled", True):
            return "Gaming Awareness está desactivado en config."
        mode_labels = {"auto": "automático", "on": "forzado", "off": "desactivado manualmente"}
        lines = [
            "Gaming Awareness",
            f"- Modo: {mode_labels.get(str(report.get('mode')), report.get('mode'))} · {'activo' if report.get('active') else 'normal'}",
        ]
        game = report.get("game") or {}
        if game:
            lines.append(f"- Juego: {game.get('process') or '?'} · señal {game.get('source') or '?'}")
            if game.get("reason"):
                lines.append(f"- Detección: {game.get('reason')}")
        lines.append(
            f"- Política LLM: {report.get('release_policy')} · "
            + ("mantener Qwen cargado" if report.get("keep_llm_loaded_during_game") else "priorizar VRAM del juego")
        )
        if report.get("release_reason"):
            lines.append(f"- Decisión VRAM: {report.get('release_reason')}")
        if report.get("llm_released"):
            reclaimed = _safe_float(report.get("vram_reclaimed_mb"))
            lines.append(f"- Qwen liberado por Gaming Mode" + (f" · ~{reclaimed:.0f} MB recuperados" if reclaimed else ""))
        gpu = report.get("gpu") or {}
        if gpu.get("vram_total_mb"):
            lines.append(
                f"- GPU: {gpu.get('utilization', '?')}% · VRAM {gpu.get('vram_used_mb', '?')}/{gpu.get('vram_total_mb', '?')} MB "
                f"({gpu.get('vram_percent', '?')}%)"
            )
        if report.get("perception_poll_ms"):
            lines.append(f"- Perception: {report.get('perception_poll_ms')} ms")
        if report.get("last_action"):
            lines.append(f"- Última acción: {report.get('last_action')}")
        return "\n".join(lines)


_INSTANCE: GamingAwarenessManager | None = None
_INSTANCE_LOCK = threading.Lock()


def get_gaming_awareness(config: dict[str, Any] | None = None, **kwargs) -> GamingAwarenessManager:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = GamingAwarenessManager(config or {}, **kwargs)
        elif isinstance(config, dict):
            _INSTANCE.update_config(config)
        return _INSTANCE
