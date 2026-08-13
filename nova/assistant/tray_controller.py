from __future__ import annotations

"""Pystray adapter for Nova Resident Mode.

The tray thread never manipulates Tk directly. Window operations are delegated
to RuntimeLifecycleManager and slow Qwen/update actions run on worker threads.
"""

import threading
import time
from typing import Any, Callable


class TrayController:
    def __init__(self, ui, lifecycle, icon_factory=None):
        self.ui = ui
        self.lifecycle = lifecycle
        self.icon_factory = icon_factory
        self.icon = None
        self.available = False
        self.degraded = False
        self.last_error = ""
        self._notify_last: dict[str, float] = {}
        self._notify_lock = threading.Lock()

    def start(self) -> bool:
        if self.available:
            return True
        try:
            self.icon = self.icon_factory(self) if self.icon_factory else self._default_icon()
            self.icon.run_detached()
            self.available = True
            self.degraded = False
            self.last_error = ""
            return True
        except Exception as exc:
            self.icon = None
            self.available = False
            self.degraded = True
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            return False

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
            if self.icon is not None:
                self.icon.update_menu()
        except Exception:
            return

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
        if not manager.set_enabled(enabled):
            raise RuntimeError("No se pudo modificar el inicio con Windows")
        resident = self.ui.config.setdefault("resident_mode", {})
        resident["start_with_windows"] = enabled
        from .config import save_config
        save_config(self.ui.config)

    def notify(self, key: str, title: str, message: str) -> bool:
        # Base UI historically reports every inference as a completed result.
        # Resident Mode intentionally ignores that generic signal; only the
        # TaskEngine emits the dedicated long_task_completed notification.
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
            # Mensajes de bandeja son deliberadamente genéricos: nunca incluyen
            # prompts, títulos de ventanas ni contenido de pantalla.
            self.icon.notify(str(message)[:220], str(title)[:80])
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            return False

    def stop(self) -> None:
        icon = self.icon
        self.icon = None
        self.available = False
        if icon is not None:
            icon.stop()

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "degraded": self.degraded, "last_error": self.last_error}
