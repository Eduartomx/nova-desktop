from __future__ import annotations

"""Thin pystray adapter. UI actions are delegated back to RuntimeLifecycleManager."""

from typing import Any


class TrayController:
    def __init__(self, ui, lifecycle, icon_factory=None):
        self.ui = ui
        self.lifecycle = lifecycle
        self.icon_factory = icon_factory
        self.icon = None
        self.available = False
        self.degraded = False
        self.last_error = ""

    def start(self) -> bool:
        if self.available:
            return True
        try:
            self.icon = self.icon_factory(self) if self.icon_factory else self._default_icon()
            self.icon.run_detached()
            self.available = True
            self.degraded = False
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
            pystray.MenuItem("Salir de Nova", lambda *_: self.lifecycle.request_shutdown("tray_exit")),
        )
        return pystray.Icon("Nova", image, "Nova", menu)

    def notify(self, _key: str, title: str, message: str) -> bool:
        if not self.available or self.icon is None:
            return False
        try:
            self.icon.notify(str(message)[:220], str(title)[:80])
            return True
        except Exception:
            return False

    def stop(self) -> None:
        icon = self.icon
        self.icon = None
        self.available = False
        if icon is not None:
            icon.stop()

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "degraded": self.degraded, "last_error": self.last_error}
