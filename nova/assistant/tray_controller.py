from __future__ import annotations

"""Pystray adapter for Nova Resident Mode.

The tray thread never manipulates Tk directly. ``available`` is only set after
pystray's setup callback makes the icon visible and confirms that visibility.
Each start attempt owns a generation/event so late callbacks cannot validate a
newer attempt.
"""

import threading
import time
from typing import Any, Callable


class TrayController:
    def __init__(self, ui, lifecycle, icon_factory=None, *, ready_timeout: float = 3.0):
        self.ui = ui
        self.lifecycle = lifecycle
        self.icon_factory = icon_factory
        self.icon = None
        self.available = False
        self.degraded = False
        self.last_error = ""
        self.ready_timeout = max(0.1, float(ready_timeout))
        self._ready = threading.Event()
        self._notify_last: dict[str, float] = {}
        self._notify_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._generation = 0
        self._active_generation = 0

    def _attempt_current(self, generation: int, icon) -> bool:
        with self._state_lock:
            return self._active_generation == int(generation) and self.icon is icon

    def _restore_hidden_window(self) -> None:
        try:
            hidden = bool(getattr(self.lifecycle, "window_hidden", False)) or str(getattr(self.lifecycle, "state", "")) == "hidden"
            if hidden:
                # RuntimeLifecycleManager.show_window() schedules _show_now on Tk.
                # Never touch Tk widgets from the pystray/backend thread here.
                self.lifecycle.show_window()
        except Exception:
            pass

    def _degrade(self, detail: str, *, generation: int | None = None, icon=None) -> bool:
        with self._state_lock:
            if generation is not None:
                if self._active_generation != int(generation):
                    return False
                if icon is not None and self.icon is not icon:
                    return False
            current_icon = icon if icon is not None else self.icon
            if icon is None or self.icon is current_icon:
                self.icon = None
            self.available = False
            self.degraded = True
            self.last_error = str(detail or "tray unavailable")[:240]
            if generation is None or self._active_generation == int(generation):
                self._active_generation = 0
            ready = self._ready
            ready.clear()
        if current_icon is not None:
            try:
                current_icon.stop()
            except Exception:
                pass
        self._restore_hidden_window()
        return False

    def start(self) -> bool:
        with self._start_lock:
            with self._state_lock:
                if self.available:
                    return True
                self._generation += 1
                generation = self._generation
                ready = threading.Event()
                self._ready = ready
                self._active_generation = generation
                self.degraded = False
                self.last_error = ""

            icon = None
            outcome = {"ok": False, "error": ""}
            try:
                icon = self.icon_factory(self) if self.icon_factory else self._default_icon()
                with self._state_lock:
                    if self._active_generation != generation:
                        return False
                    self.icon = icon

                def setup(callback_icon):
                    # A callback belonging to an expired attempt is inert. In
                    # particular it must not set visible=True after timeout.
                    if callback_icon is not icon or not self._attempt_current(generation, icon):
                        return
                    try:
                        callback_icon.visible = True
                        if not bool(getattr(callback_icon, "visible", False)):
                            raise RuntimeError("tray icon did not become visible")
                    except Exception as exc:
                        with self._state_lock:
                            if self._active_generation == generation and self.icon is icon:
                                outcome["error"] = f"{type(exc).__name__}: {exc}"
                                ready.set()
                        return
                    with self._state_lock:
                        if self._active_generation == generation and self.icon is icon:
                            outcome["ok"] = True
                            ready.set()

                try:
                    icon.run_detached(setup=setup)
                except TypeError:
                    # Compatibility only for explicitly injected legacy doubles.
                    # Production pystray receives the setup callback above.
                    if self.icon_factory is None:
                        raise
                    icon.run_detached()
                    setup(icon)

                if not ready.wait(self.ready_timeout):
                    return self._degrade("tray initialization timeout", generation=generation, icon=icon)
                if outcome["error"]:
                    return self._degrade(outcome["error"], generation=generation, icon=icon)
                if not outcome["ok"] or not bool(getattr(icon, "visible", False)):
                    return self._degrade("tray backend did not make icon visible", generation=generation, icon=icon)

                with self._state_lock:
                    if self._active_generation != generation or self.icon is not icon:
                        return False
                    self.available = True
                    self.degraded = False
                    self.last_error = ""
                return True
            except Exception as exc:
                return self._degrade(f"{type(exc).__name__}: {exc}", generation=generation, icon=icon)

    def _default_icon(self):
        import pystray
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((5, 5, 59, 59), fill=(45, 55, 70, 255))
        draw.text((21, 16), "N", fill=(255, 255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Abrir Nova", lambda *_: self.lifecycle.show_window(), default=True),
            pystray.MenuItem(lambda _item: self.qwen_text(), None, enabled=False),
            pystray.MenuItem(lambda _item: self.gaming_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Precargar Qwen", lambda *_: self._run_worker(self._preload_qwen)),
            pystray.MenuItem("Liberar Qwen", lambda *_: self._run_worker(self._unload_qwen)),
            pystray.MenuItem("Buscar actualizaciones", lambda *_: self._run_worker(self._check_updates)),
            pystray.MenuItem(
                "Iniciar con Windows",
                lambda *_: self._run_worker(self._toggle_autostart),
                checked=lambda _item: bool(self.ui.autostart_manager.is_enabled()),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir de Nova", lambda *_: self.lifecycle.request_shutdown("tray_exit")),
        )
        return pystray.Icon("Nova", image, "Nova", menu)

    def qwen_text(self) -> str:
        warm = getattr(self.ui, "llm_warm_manager", None) or getattr(getattr(self.ui, "agent", None), "llm_warm", None)
        if warm is None:
            return "Qwen: no disponible"
        try:
            report = warm.cached_status()
            if report.get("warming"):
                return "Qwen: precargando"
            if report.get("loaded"):
                vram = float(report.get("size_vram_mb") or 0)
                return f"Qwen: listo · {vram:.0f} MB" if vram else "Qwen: listo"
            return "Qwen: descargado"
        except Exception:
            return "Qwen: estado no disponible"

    def gaming_text(self) -> str:
        manager = getattr(self.ui, "gaming_awareness", None)
        if manager is None:
            return "Gaming: preparado"
        try:
            report = manager.status(refresh=False)
            if report.get("active"):
                game = report.get("game") or {}
                return "Gaming: activo · " + str(game.get("process") or "juego")
            return "Gaming: normal"
        except Exception:
            return "Gaming: estado no disponible"

    def refresh_menu(self) -> None:
        try:
            if self.icon is not None and self.available:
                self.icon.update_menu()
        except Exception as exc:
            self._degrade(f"menu refresh failed: {type(exc).__name__}: {exc}")

    def _run_worker(self, callback: Callable[[], Any]) -> None:
        threading.Thread(target=self._worker, args=(callback,), daemon=True, name="nova-tray-action").start()

    def _worker(self, callback) -> None:
        try:
            callback()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            self.notify("important_error", "Nova necesita atención", "Ocurrió un error importante. Abre Nova para revisarlo.")
        finally:
            self.refresh_menu()

    def _preload_qwen(self) -> None:
        warm = getattr(self.ui, "llm_warm_manager", None) or getattr(getattr(self.ui, "agent", None), "llm_warm", None)
        if warm is not None:
            warm.preload(reason="tray")

    def _unload_qwen(self) -> None:
        warm = getattr(self.ui, "llm_warm_manager", None) or getattr(getattr(self.ui, "agent", None), "llm_warm", None)
        if warm is not None:
            warm.unload(reason="tray", force=False)

    def _check_updates(self) -> None:
        from updater.nova_updater import get_release, load_config, version_key, version_text

        current = version_text()
        release = get_release(load_config())
        latest = str(release.get("tag_name") or "").lstrip("vV")
        if latest and version_key(latest) > version_key(current):
            self.notify("update_available", "Actualización disponible", f"Nova {latest} está disponible.")

    def _toggle_autostart(self) -> None:
        manager = self.ui.autostart_manager
        enabled = not manager.is_enabled()
        result = manager.configure(enabled)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "No se pudo modificar el inicio con Windows"))
        resident = self.ui.config.setdefault("resident_mode", {})
        resident["start_with_windows"] = enabled
        from .config import save_config
        save_config(self.ui.config)

    def notify(self, key: str, title: str, message: str) -> bool:
        if str(key) == "task_completed":
            return False
        resident = self.ui.config.get("resident_mode", {}) if isinstance(self.ui.config, dict) else {}
        if not bool(resident.get("notifications", True)) or not self.available or self.icon is None:
            return False
        now = time.monotonic()
        with self._notify_lock:
            previous = self._notify_last.get(str(key), 0.0)
            if now - previous < 45.0:
                return False
            self._notify_last[str(key)] = now
        try:
            self.icon.notify(str(message)[:220], str(title)[:80])
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            return False

    def stop(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._active_generation = 0
            icon = self.icon
            self.icon = None
            self.available = False
            self._ready.clear()
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            ready = bool(self._ready.is_set() and self.available and self._active_generation)
            generation = int(self._active_generation)
        return {
            "available": self.available,
            "ready": ready,
            "degraded": self.degraded,
            "last_error": self.last_error,
            "generation": generation,
        }
