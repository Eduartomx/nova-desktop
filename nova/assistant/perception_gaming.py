from __future__ import annotations

"""Extensión pequeña de Perception para throttling temporal en Gaming Mode."""

import time


def install_perception_gaming():
    from .perception import PerceptionEngine

    if getattr(PerceptionEngine, "_nova_gaming_poll_patched", False):
        return PerceptionEngine

    original_init = PerceptionEngine.__init__

    def init(self, *args, **kwargs):
        self._runtime_poll_interval_ms = None
        original_init(self, *args, **kwargs)

    def set_runtime_poll_interval_ms(self, value):
        if value is None:
            self._runtime_poll_interval_ms = None
        else:
            self._runtime_poll_interval_ms = max(250, int(value))
        return self.effective_poll_interval_ms()

    def effective_poll_interval_ms(self):
        value = getattr(self, "_runtime_poll_interval_ms", None)
        if value is not None:
            return int(value)
        return max(250, int(self.config.get("poll_interval_ms", 1100)))

    def loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.sample_once()
            except Exception:
                pass
            elapsed = time.monotonic() - started
            interval = self.effective_poll_interval_ms() / 1000.0
            self._stop.wait(max(0.05, interval - elapsed))

    PerceptionEngine.__init__ = init
    PerceptionEngine.set_runtime_poll_interval_ms = set_runtime_poll_interval_ms
    PerceptionEngine.effective_poll_interval_ms = effective_poll_interval_ms
    PerceptionEngine._loop = loop
    PerceptionEngine._nova_gaming_poll_patched = True
    return PerceptionEngine
