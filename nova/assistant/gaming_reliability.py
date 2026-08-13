from __future__ import annotations

"""Confiabilidad de ciclo de vida para Gaming Awareness.

Esta capa refuerza identidad de procesos, frescura de Perception, eventos de UI y
restauraciones de Qwen sin convertir Nova 0.9.x en una reescritura general.
"""

import threading
import time
from typing import Any, Callable

import psutil

from .gaming_awareness import GamingAwarenessManager
from .gaming_detection_filters import is_mandatory_gaming_exclusion


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold().replace("/", "\\")


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _game_key(game: dict[str, Any] | None) -> tuple[int, str, float]:
    game = game or {}
    return (
        int(game.get("pid") or 0),
        _norm(game.get("process") or ""),
        round(_safe_float(game.get("create_time")), 3),
    )


def install_gaming_reliability():
    Manager = GamingAwarenessManager
    if getattr(Manager, "_nova_gaming_reliability_patched", False):
        return Manager

    original_init = Manager.__init__
    original_update_config = Manager.update_config
    original_enter = Manager._enter
    original_exit = Manager._exit
    original_status = Manager.status
    original_stop = Manager.stop

    def update_config(self, config=None):
        original_update_config(self, config)
        cfg = self.gaming_config
        cfg.setdefault("perception_max_age_seconds", 6.0)
        cfg.setdefault("ignored_game_processes", [])
        cfg.setdefault("ignored_game_path_markers", [])
        return self

    def init(self, *args, **kwargs):
        self._gaming_identity_sensor = kwargs.pop("identity_sensor", None)
        original_init(self, *args, **kwargs)
        self._gaming_state_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._gaming_listener_lock = threading.RLock()
        self._gaming_candidate_key: tuple[int, str, float] | None = None
        self._tracked_game_identity: dict[str, Any] = {}
        self._restore_generation = 0
        self._restore_inflight_generation = 0
        self._gaming_restore_transition_lock = threading.RLock()

    def add_state_listener(self, callback):
        if not callable(callback):
            raise TypeError("callback debe ser invocable")
        with self._gaming_listener_lock:
            if callback not in self._gaming_state_listeners:
                self._gaming_state_listeners.append(callback)
        return callback

    def remove_state_listener(self, callback):
        with self._gaming_listener_lock:
            try:
                self._gaming_state_listeners.remove(callback)
            except ValueError:
                pass

    def _emit_state(self, event: str):
        try:
            report = self.status(refresh=False)
        except Exception:
            return
        with self._gaming_listener_lock:
            listeners = list(self._gaming_state_listeners)
        for callback in listeners:
            try:
                callback(str(event), dict(report))
            except Exception:
                continue

    def _identity(self, pid: int, expected_process: str = "") -> dict[str, Any] | None:
        pid = int(pid or 0)
        if pid <= 0:
            return None
        sensor = getattr(self, "_gaming_identity_sensor", None)
        if callable(sensor):
            try:
                raw = sensor(pid)
            except Exception:
                raw = None
            if not isinstance(raw, dict) or not raw.get("alive", True):
                return None
            name = str(raw.get("process") or raw.get("name") or expected_process or "")
            if expected_process and name and _norm(name) != _norm(expected_process):
                return None
            return {
                "pid": pid,
                "process": name,
                "exe": str(raw.get("exe") or ""),
                "create_time": _safe_float(raw.get("create_time")),
            }
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return None
            try:
                if proc.status() == psutil.STATUS_ZOMBIE:
                    return None
            except Exception:
                pass
            name = str(proc.name() or "")
            if expected_process and name and _norm(name) != _norm(expected_process):
                return None
            try:
                exe = str(proc.exe() or "")
            except Exception:
                exe = ""
            try:
                created = float(proc.create_time() or 0)
            except Exception:
                created = 0.0
            return {"pid": pid, "process": name or expected_process, "exe": exe, "create_time": created}
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        except Exception:
            return None

    def _explicit_processes(self) -> set[str]:
        return {_norm(x) for x in self.gaming_config.get("game_processes", []) if str(x).strip()}

    def _ignored_processes(self) -> set[str]:
        return {_norm(x) for x in self.gaming_config.get("ignored_game_processes", []) if str(x).strip()}

    def _ignored_paths(self) -> list[str]:
        return [_norm(x) for x in self.gaming_config.get("ignored_game_path_markers", []) if str(x).strip()]

    def _path_markers(self) -> list[str]:
        return [_norm(x) for x in self.gaming_config.get("game_path_markers", []) if str(x).strip()]

    def _is_ignored(self, process: str, exe: str = "") -> bool:
        if is_mandatory_gaming_exclusion(process, exe):
            return True
        proc = _norm(process)
        if proc and proc in self._explicit_processes():
            return False
        if proc and proc in self._ignored_processes():
            return True
        norm_exe = _norm(exe)
        return bool(norm_exe and any(marker and marker in norm_exe for marker in self._ignored_paths()))

    def _perception_game(self) -> dict[str, Any] | None:
        try:
            state = self._perception_engine().current(refresh=False)
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        sampled_at = _safe_float(state.get("sampled_at"))
        max_age = max(1.0, _safe_float(self.gaming_config.get("perception_max_age_seconds", 6.0)) or 6.0)
        if sampled_at and time.time() - sampled_at > max_age:
            return None

        foreground = state.get("foreground") if isinstance(state.get("foreground"), dict) else {}
        external = state.get("external") if isinstance(state.get("external"), dict) else {}
        candidates: list[tuple[dict[str, Any], bool]] = []
        if foreground and not foreground.get("is_nova"):
            candidates.append((foreground, True))
        if external and int(external.get("pid") or 0) != int(foreground.get("pid") or 0):
            candidates.append((external, False))

        for item, is_foreground in candidates:
            pid = int(item.get("pid") or 0)
            process = str(item.get("process") or "")
            identity = self._identity(pid, process)
            if identity is None:
                continue
            exe = str(item.get("exe") or identity.get("exe") or "")
            if self._is_ignored(process, exe):
                continue
            base = {
                "pid": pid,
                "process": process or identity.get("process") or "",
                "title": str(item.get("title") or "")[:180],
                "create_time": identity.get("create_time") or 0.0,
                "foreground": bool(is_foreground),
            }
            if str(item.get("app_kind") or "") == "game":
                base.update({
                    "source": "foreground" if is_foreground else "perception",
                    "reason": "Perception Engine clasificó una ventana viva como juego",
                })
                return base

            norm_exe = _norm(exe)
            if is_foreground and norm_exe and any(marker and marker in norm_exe for marker in self._path_markers()):
                base.update({
                    "source": "foreground_game_path",
                    "reason": "ventana en primer plano dentro de una biblioteca de juegos",
                })
                return base
        return None

    def _scan_processes(self) -> dict[str, Any] | None:
        if self._process_sensor is not None:
            try:
                found = self._process_sensor()
            except Exception:
                found = None
            if not isinstance(found, dict):
                return None
            process = str(found.get("process") or "")
            if self._is_ignored(process, str(found.get("exe") or "")):
                return None
            pid = int(found.get("pid") or 0)
            if pid:
                identity = self._identity(pid, process)
                if identity is None:
                    return None
                found = dict(found)
                found.setdefault("create_time", identity.get("create_time") or 0.0)
            return dict(found)

        configured = self._explicit_processes()
        mc_markers = [str(x).casefold() for x in self.gaming_config.get("minecraft_command_markers", []) if str(x).strip()]
        try:
            rows = psutil.process_iter(["pid", "name", "exe", "cmdline", "create_time"])
        except Exception:
            return None
        for proc in rows:
            try:
                info = proc.info
                pid = int(info.get("pid") or 0)
                name = str(info.get("name") or "")
                norm_name = _norm(name)
                exe = str(info.get("exe") or "")
                cmdline = " ".join(str(x) for x in (info.get("cmdline") or [])).casefold()
                created = _safe_float(info.get("create_time"))

                if self._is_ignored(name, exe):
                    continue
                if norm_name in configured:
                    return {
                        "pid": pid,
                        "process": name,
                        "exe": exe,
                        "create_time": created,
                        "source": "process",
                        "reason": "proceso configurado como juego",
                        "foreground": False,
                    }
                if norm_name == "javaw.exe" and any(marker in cmdline for marker in mc_markers):
                    return {
                        "pid": pid,
                        "process": name,
                        "exe": exe,
                        "create_time": created,
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
        game = self._perception_game()
        if game:
            return game
        return self._scan_processes()

    def _tracked_alive(self) -> bool:
        game = dict(getattr(self, "_tracked_game_identity", {}) or {})
        if not game:
            return False
        if str(game.get("source") or "") == "manual" and self._manual_mode == "on":
            return True
        pid = int(game.get("pid") or 0)
        if pid <= 0:
            return False
        live = self._identity(pid, str(game.get("process") or ""))
        if live is None:
            return False
        expected_created = _safe_float(game.get("create_time"))
        live_created = _safe_float(live.get("create_time"))
        if expected_created and live_created and abs(expected_created - live_created) > 0.05:
            return False
        return True

    def _apply_game_warm_guards(self, warm=None):
        warm = warm or self._warm_manager()
        if bool(self.gaming_config.get("keep_llm_loaded_during_game", False)):
            warm.clear_preload_suppression("gaming_mode")
            warm.clear_runtime_keep_alive_override("gaming_mode")
            return
        warm.suppress_preload("gaming_mode")
        warm.set_runtime_keep_alive_override(
            self.gaming_config.get("llm_keep_alive_during_game", 0),
            reason="gaming_mode",
        )

    def _invalidate_restore_locked(self):
        with self._lock:
            self._restore_generation += 1
            timer = self._restore_timer
            self._restore_timer = None
            return timer

    def _cancel_restore(self):
        with self._gaming_restore_transition_lock:
            timer = self._invalidate_restore_locked()
        try:
            if timer is not None:
                timer.cancel()
        except Exception:
            pass

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
        with self._gaming_restore_transition_lock:
            with self._lock:
                self._restore_generation += 1
                generation = self._restore_generation
                previous = self._restore_timer

            try:
                if previous is not None:
                    previous.cancel()
            except Exception:
                pass

            def restore():
                with self._gaming_restore_transition_lock:
                    with self._lock:
                        if generation != self._restore_generation or self._active or self._manual_mode == "on":
                            return
                        self._restore_inflight_generation = generation
                    warm.clear_preload_suppression("gaming_mode")
                    warm.clear_runtime_keep_alive_override("gaming_mode")
                    with self._lock:
                        stale_before_preload = (
                            generation != self._restore_generation
                            or self._active
                            or self._manual_mode == "on"
                        )
                    if stale_before_preload:
                        if self._active:
                            self._apply_game_warm_guards(warm)
                        with self._lock:
                            if self._restore_inflight_generation == generation:
                                self._restore_inflight_generation = 0
                        return

                if warm.preload_on_start:
                    report = warm.preload(reason="gaming_restore")
                else:
                    report = warm.cached_status()

                with self._gaming_restore_transition_lock:
                    with self._lock:
                        stale = (
                            generation != self._restore_generation
                            or self._active
                            or self._manual_mode == "on"
                        )
                        active_now = bool(self._active)

                    if stale:
                        if active_now:
                            self._apply_game_warm_guards(warm)
                            warm_state = warm.cached_status()
                            keep_loaded = bool(self.gaming_config.get("keep_llm_loaded_during_game", False))
                            can_compensate = (
                                not keep_loaded
                                and bool(warm_state.get("loaded"))
                                and not bool(warm_state.get("warming"))
                                and int(warm_state.get("active_inferences") or 0) <= 0
                            )
                            if can_compensate:
                                unload_report = warm.unload(timeout=4.0, reason="gaming_mode")
                                self._apply_game_warm_guards(warm)
                                if unload_report.get("loaded") is False:
                                    with self._lock:
                                        if self._active:
                                            self._llm_released = True
                                            self._last_action = "Qwen liberado tras cancelar una restauración obsoleta"
                                            try:
                                                from .gaming_awareness import _utc_now
                                                self._last_action_at = _utc_now()
                                            except Exception:
                                                self._last_action_at = ""
                        with self._lock:
                            if self._restore_inflight_generation == generation:
                                self._restore_inflight_generation = 0
                            if generation == self._restore_generation:
                                self._restore_timer = None
                        return

                    with self._lock:
                        self._last_action = (
                            "Qwen precargado de nuevo al salir del juego"
                            if report.get("loaded")
                            else "Qwen quedó bajo demanda al salir del juego"
                        )
                        try:
                            from .gaming_awareness import _utc_now
                            self._last_action_at = _utc_now()
                        except Exception:
                            self._last_action_at = ""
                        self._restore_timer = None
                        self._restore_inflight_generation = 0
                    self._emit_state("resources_changed")

            timer = threading.Timer(delay, restore)
            timer.daemon = True
            with self._lock:
                if generation != self._restore_generation:
                    return
                self._restore_timer = timer
            timer.start()

    def _enter(self, game):
        game = dict(game or {})
        warm = self._warm_manager()
        with self._gaming_restore_transition_lock:
            timer = self._invalidate_restore_locked()
            self._apply_game_warm_guards(warm)
        try:
            if timer is not None:
                timer.cancel()
        except Exception:
            pass

        was_active = bool(self._active)
        old_key = _game_key(self._game)
        original_enter(self, game)

        with self._gaming_restore_transition_lock:
            if self._active:
                self._apply_game_warm_guards(warm)

        with self._lock:
            self._tracked_game_identity = dict(game)
            self._gaming_candidate_key = None
            self._clear_since = 0.0
        new_key = _game_key(game)
        if not was_active:
            self._emit_state("entered")
        elif new_key != old_key:
            with self._lock:
                self._last_action = f"Juego activo cambiado a {game.get('process') or '?'}"
            self._emit_state("game_changed")

    def _exit(self, reason="juego cerrado"):
        was_active = bool(self._active)
        with self._gaming_restore_transition_lock:
            original_exit(self, reason)
        if was_active:
            with self._lock:
                self._tracked_game_identity = {}
                self._gaming_candidate_key = None
            self._emit_state("exited")

    def tick(self):
        if not self.enabled:
            if self._active:
                self._exit("Gaming Awareness desactivado")
            return self.status(refresh=False)

        mode = self._manual_mode
        now = time.monotonic()
        if mode == "on":
            if not self._active:
                self._enter({
                    "pid": 0,
                    "process": "manual",
                    "source": "manual",
                    "reason": "Gaming Mode forzado por el usuario",
                    "foreground": True,
                })
            else:
                self._apply_active_policy()
            return self.status(refresh=False)

        if mode == "off" or not bool(self.gaming_config.get("auto_detect", True)):
            if self._active:
                self._exit("detección automática desactivada")
            return self.status(refresh=False)

        detected = self.detect_game()
        if self._active:
            tracked_alive = self._tracked_alive()
            if detected:
                with self._lock:
                    self._clear_since = 0.0
                if not tracked_alive or _game_key(detected) != _game_key(self._tracked_game_identity):
                    self._enter(detected)
                else:
                    with self._lock:
                        self._game = dict(detected)
                        self._tracked_game_identity = dict(detected)
                self._apply_active_policy()
                return self.status(refresh=False)

            if tracked_alive:
                with self._lock:
                    self._clear_since = 0.0
                self._apply_active_policy()
                return self.status(refresh=False)

            if not self._clear_since:
                self._clear_since = now
            dwell = max(0.0, float(self.gaming_config.get("exit_dwell_seconds", 7.0) or 0.0))
            if now - self._clear_since >= dwell:
                self._exit("el proceso del juego ya no existe")
            return self.status(refresh=False)

        if detected:
            key = _game_key(detected)
            if self._gaming_candidate_key != key:
                self._gaming_candidate_key = key
                self._candidate_since = now
            dwell = max(0.0, float(self.gaming_config.get("enter_dwell_seconds", 2.5) or 0.0))
            if now - self._candidate_since >= dwell:
                self._enter(detected)
        else:
            self._candidate_since = 0.0
            self._gaming_candidate_key = None
        return self.status(refresh=False)

    def status(self, refresh=False):
        report = dict(original_status(self, refresh=refresh))
        with self._lock:
            report["tracked_game_identity"] = dict(self._tracked_game_identity)
            report["restore_generation"] = int(self._restore_generation)
            report["restore_inflight_generation"] = int(self._restore_inflight_generation)
        return report

    def stop(self, timeout=1.0):
        result = original_stop(self, timeout=timeout)
        self._cancel_restore()
        return result

    Manager.update_config = update_config
    Manager.__init__ = init
    Manager.add_state_listener = add_state_listener
    Manager.remove_state_listener = remove_state_listener
    Manager._emit_state = _emit_state

    Manager._identity = _identity
    Manager._explicit_processes = _explicit_processes
    Manager._ignored_processes = _ignored_processes
    Manager._ignored_paths = _ignored_paths
    Manager._path_markers = _path_markers
    Manager._is_ignored = _is_ignored
    Manager._perception_game = _perception_game
    Manager._tracked_alive = _tracked_alive
    Manager._apply_game_warm_guards = _apply_game_warm_guards
    Manager._invalidate_restore_locked = _invalidate_restore_locked
    Manager._cancel_restore = _cancel_restore

    Manager._gaming_identity = _identity
    Manager._gaming_explicit_processes = _explicit_processes
    Manager._gaming_ignored_processes = _ignored_processes
    Manager._gaming_ignored_paths = _ignored_paths
    Manager._gaming_path_markers = _path_markers
    Manager._gaming_is_ignored = _is_ignored
    Manager._gaming_tracked_alive = _tracked_alive
    Manager._gaming_cancel_restore = _cancel_restore

    Manager._foreground_game = _perception_game
    Manager._scan_processes = _scan_processes
    Manager.detect_game = detect_game
    Manager._schedule_restore = _schedule_restore
    Manager._enter = _enter
    Manager._exit = _exit
    Manager.tick = tick
    Manager.status = status
    Manager.stop = stop
    Manager._nova_gaming_reliability_patched = True
    return Manager
