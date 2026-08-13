from __future__ import annotations

import unittest

from assistant.gaming_awareness import GamingAwarenessManager
from assistant.gaming_status_sync import install_gaming_status_sync
from assistant.ui_gaming import _warm_label


class FakePerception:
    def effective_poll_interval_ms(self):
        return 1100


class GamingStateSyncTests(unittest.TestCase):
    def setUp(self):
        install_gaming_status_sync()

    def test_inactive_mode_does_not_expose_previous_release_as_current(self):
        manager = GamingAwarenessManager(
            {"gaming_awareness": {"enabled": True}},
            perception=FakePerception(),
            process_sensor=lambda: None,
            gpu_sensor=lambda: None,
        )
        manager._active = False
        manager._release_reason = "VRAM en 65.1%"
        manager._llm_released = True
        manager._last_vram_reclaimed_mb = 4035.0

        report = manager.status(refresh=False)
        self.assertFalse(report["active"])
        self.assertFalse(report["llm_released"])
        self.assertEqual(report["release_reason"], "")
        self.assertEqual(report["vram_reclaimed_mb"], 0.0)
        self.assertEqual(report["last_release_reason"], "VRAM en 65.1%")
        self.assertEqual(report["last_release_vram_reclaimed_mb"], 4035.0)

        text = manager.format_status(report)
        self.assertNotIn("Qwen liberado por Gaming Mode", text)
        self.assertIn("Última liberación: VRAM en 65.1%", text)

    def test_warm_label_returns_to_ready_after_game(self):
        self.assertEqual(
            _warm_label({"loaded": True, "size_vram_mb": 3187.1}),
            "LLM: listo · 3187 MB VRAM",
        )
        self.assertEqual(_warm_label({"warming": True}), "LLM: precargando…")
        self.assertEqual(_warm_label({"loaded": False}), "LLM: descargado")


if __name__ == "__main__":
    unittest.main()
