from __future__ import annotations

import unittest

from assistant.config import DEFAULT_CONFIG


class WorkspaceAutoDetectConfigTests(unittest.TestCase):
    def test_safe_defaults(self):
        cfg = DEFAULT_CONFIG["workspace_autodetect"]
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["learn_enabled"])
        self.assertFalse(cfg["auto_activate"])
        self.assertGreaterEqual(cfg["minimum_confirmations"], 3)
        self.assertGreaterEqual(cfg["suggestion_threshold"], 0.80)
        self.assertNotIn("browser", cfg["learn_app_kinds"])

    def test_context_intelligence_has_explicit_defaults(self):
        cfg = DEFAULT_CONFIG["context_intelligence"]
        self.assertTrue(cfg["enabled"])
        self.assertFalse(cfg["include_window_title_in_prompt"])


if __name__ == "__main__":
    unittest.main()
