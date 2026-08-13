from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from assistant.agent_instant_wake import warm_direct_intent
from assistant.hotkeys import (
    DEFAULT_CONTEXT_HOTKEY,
    DEFAULT_MAIN_HOTKEY,
    humanize_hotkey,
    normalize_hotkey,
    validate_hotkey,
)
from assistant.llm_warm import LLMWarmManager


class HotkeyTests(unittest.TestCase):
    def test_new_defaults_do_not_use_space(self):
        self.assertEqual(DEFAULT_MAIN_HOTKEY, "<ctrl>+<alt>+n")
        self.assertEqual(DEFAULT_CONTEXT_HOTKEY, "<ctrl>+<alt>+<shift>+n")
        self.assertNotIn("space", DEFAULT_MAIN_HOTKEY)
        self.assertNotIn("space", DEFAULT_CONTEXT_HOTKEY)

    def test_human_input_is_normalized_for_pynput(self):
        self.assertEqual(normalize_hotkey("Ctrl+Alt+N"), "<ctrl>+<alt>+n")
        self.assertEqual(normalize_hotkey("Control+Alt+Shift+N"), "<ctrl>+<alt>+<shift>+n")
        self.assertEqual(humanize_hotkey("<ctrl>+<alt>+n"), "Ctrl+Alt+N")

    def test_invalid_hotkeys_are_rejected(self):
        ok, error, _ = validate_hotkey("N")
        self.assertFalse(ok)
        self.assertIn("al menos dos", error)
        ok, error, _ = validate_hotkey("Ctrl+Alt+Delete")
        self.assertFalse(ok)
        self.assertIn("reservado", error)


class WarmManagerTests(unittest.TestCase):
    def _manager(self):
        return LLMWarmManager({
            "model": "qwen3.5:4b",
            "ollama_host": "http://127.0.0.1:11434",
            "context_tokens": 8192,
            "llm_warm": {
                "enabled": True,
                "preload_on_start": True,
                "startup_delay_seconds": 0,
                "keep_alive": "20m",
                "request_timeout_seconds": 1,
                "status_timeout_seconds": 1,
                "unload_on_exit": True,
            },
        })

    @staticmethod
    def _response(payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    @patch("assistant.llm_warm.requests.get")
    @patch("assistant.llm_warm.requests.post")
    def test_preload_is_empty_local_request_and_reports_vram(self, post, get):
        post.return_value = self._response({"done": True, "done_reason": "load"})
        get.return_value = self._response({
            "models": [{
                "name": "qwen3.5:4b",
                "model": "qwen3.5:4b",
                "size_vram": 6607 * 1024 * 1024,
                "expires_at": "2099-01-01T00:00:00Z",
            }]
        })
        manager = self._manager()
        report = manager.preload()

        self.assertTrue(report["loaded"])
        self.assertEqual(report["size_vram_mb"], 6607.0)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["keep_alive"], "20m")
        self.assertEqual(payload["options"]["num_ctx"], 8192)

    @patch("assistant.llm_warm.requests.post")
    def test_unload_uses_keep_alive_zero(self, post):
        post.return_value = self._response({"done": True, "done_reason": "unload"})
        manager = self._manager()
        manager._last_loaded = True
        report = manager.unload()
        self.assertFalse(report["loaded"])
        self.assertEqual(post.call_args.kwargs["json"]["keep_alive"], 0)

    def test_keep_alive_is_added_to_normal_payload(self):
        manager = self._manager()
        payload = {"model": "qwen3.5:4b"}
        manager.apply_keep_alive(payload)
        self.assertEqual(payload["keep_alive"], "20m")


class WarmIntentTests(unittest.TestCase):
    def test_direct_warm_commands(self):
        self.assertEqual(warm_direct_intent("Nova, precarga Qwen"), "preload")
        self.assertEqual(warm_direct_intent("Nova, libera la VRAM"), "unload")
        self.assertEqual(warm_direct_intent("¿Qwen está cargado?"), "status")
        self.assertIsNone(warm_direct_intent("Abre el navegador"))


if __name__ == "__main__":
    unittest.main()
