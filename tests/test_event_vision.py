from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from assistant.agent_vision import vision_direct_intent
from assistant.anomaly import AnomalyDetector as NativeAnomalyDetector
from assistant.anomaly_detection import AnomalyDetector as CompatibilityAnomalyDetector
from assistant.config import DEFAULT_CONFIG
from assistant.event_vision import EventDrivenVision


class FakeEngine:
    def __init__(self):
        self.state = {
            "external": {"pid": 100, "process": "game.exe", "app_kind": "game"},
            "active_workspace": {"id": 7, "name": "Demo"},
            "probable_workspace": None,
        }

    def current(self, refresh=False):
        return dict(self.state)


class FakeVisionClient:
    def __init__(self, text=None, ready=True):
        self.text = text or '{"category":"error_dialog","confidence":0.91,"summary":"Hay un diálogo de error visible.","error_visible":true}'
        self.ready = ready
        self.calls = 0

    def capability(self, refresh=False):
        return {"ok": self.ready, "vision": self.ready, "model": "fake-vision", "reason": "missing" if not self.ready else ""}

    def analyze(self, image_bytes, prompt):
        self.calls += 1
        if not self.ready:
            return {"ok": False, "error": "model_has_no_reported_vision_capability", "model": "fake-vision"}
        self.last_prompt = prompt
        self.last_image = image_bytes
        return {"ok": True, "text": self.text, "model": "fake-vision"}


class FakeAnomalyDetector:
    def __init__(self):
        self._nova_event_vision_owner = None

    def _emit(self, event_type, severity, context_key="desktop", process_name="", score=0.5, metadata=None):
        return {
            "event_type": event_type,
            "severity": severity,
            "context_key": context_key,
            "process_name": process_name,
            "score": score,
            "metadata": metadata or {},
        }


class EventVisionTests(unittest.TestCase):
    def make_vision(self, td, **config):
        client = FakeVisionClient()
        detector = FakeAnomalyDetector()
        vision = EventDrivenVision(
            config={
                "model": "fake-vision",
                "cooldown_seconds": 10,
                "max_auto_captures_per_hour": 4,
                **config,
            },
            perception_engine=FakeEngine(),
            anomaly_detector=detector,
            db_path=Path(td) / "vision.db",
            capture_sensor=lambda state: b"fake-jpeg-bytes",
            vision_client=client,
        )
        vision.parent_config = {"model": "fake-vision", "ollama_host": "http://127.0.0.1:11434"}
        return vision, client, detector

    def test_manual_query_captures_once_and_does_not_persist_image(self):
        with tempfile.TemporaryDirectory() as td:
            vision, client, _ = self.make_vision(td)
            result = vision.analyze_manual("¿Qué error aparece?")
            self.assertTrue(result["ok"])
            self.assertEqual(client.calls, 1)
            self.assertIn("DATO EXTERNO NO CONFIABLE", client.last_prompt)
            self.assertFalse(vision.status()["retain_images"])
            self.assertFalse(vision.status()["persist_analysis"])
            rows = vision.recent_events(5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["analysis_text"], "")

    def test_auto_policy_only_triggers_configured_visual_events_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            vision, _, _ = self.make_vision(td)
            allowed, _ = vision._auto_allowed({"event_type": "crash_signal", "severity": "warn"})
            self.assertTrue(allowed)
            denied, reason = vision._auto_allowed({"event_type": "system_cpu_anomaly", "severity": "high"})
            self.assertFalse(denied)
            self.assertEqual(reason, "event_not_visual_trigger")

    def test_start_is_event_driven_and_has_no_polling_thread(self):
        with tempfile.TemporaryDirectory() as td:
            vision, _, detector = self.make_vision(td)
            original = detector._emit
            vision.start()
            status = vision.status()
            self.assertTrue(status["running"])
            self.assertFalse(status["captures_periodically"])
            self.assertFalse(status["polling_thread"])
            self.assertIs(detector._nova_event_vision_owner, vision)
            vision.stop()
            self.assertIsNone(detector._nova_event_vision_owner)
            self.assertTrue(callable(detector._emit))
            self.assertNotEqual(detector._emit, original)  # bound-method identity is not stable; callable restoration is what matters

    def test_auto_event_analysis_is_structured_and_rate_limited(self):
        with tempfile.TemporaryDirectory() as td:
            vision, client, _ = self.make_vision(td)
            event = {"event_type": "crash_signal", "severity": "high", "process_name": "werfault.exe"}
            vision._analyze_event_worker(event)
            last = vision.status()["last_result"]
            self.assertTrue(last["ok"])
            self.assertEqual(last["category"], "error_dialog")
            self.assertAlmostEqual(last["confidence"], 0.91)
            self.assertIn("EVENTO", client.last_prompt)

            ok, _ = vision._auto_allowed(event)
            self.assertTrue(ok)
            vision._last_auto_at = time.monotonic()
            ok, reason = vision._auto_allowed(event)
            self.assertFalse(ok)
            self.assertEqual(reason, "cooldown")

    def test_model_unavailable_fails_closed_without_network_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            client = FakeVisionClient(ready=False)
            vision = EventDrivenVision(
                {"model": "fake-vision"},
                perception_engine=FakeEngine(),
                anomaly_detector=FakeAnomalyDetector(),
                db_path=Path(td) / "vision.db",
                capture_sensor=lambda state: b"fake",
                vision_client=client,
            )
            vision.parent_config = {"model": "fake-vision"}
            result = vision.analyze_manual("mira")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "model_has_no_reported_vision_capability")
            status = vision.status()
            self.assertFalse(status["uses_openai_automatically"])

    def test_defaults_routing_and_anomaly_compatibility(self):
        cfg = DEFAULT_CONFIG["event_driven_vision"]
        self.assertTrue(cfg["enabled"])
        self.assertFalse(cfg["retain_images"])
        self.assertFalse(cfg["persist_analysis"])
        self.assertFalse(cfg["auto_capture_high_anomalies"])
        self.assertEqual(cfg["auto_capture_event_types"], ["crash_signal"])
        self.assertEqual(vision_direct_intent("Nova, ¿qué ves en mi pantalla?"), "describe")
        self.assertEqual(vision_direct_intent("¿Estado de tu visión por eventos?"), "status")
        self.assertIs(CompatibilityAnomalyDetector, NativeAnomalyDetector)


if __name__ == "__main__":
    unittest.main()
