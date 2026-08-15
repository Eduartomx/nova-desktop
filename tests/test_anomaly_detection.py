from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

from assistant.agent_anomaly import anomaly_direct_intent
from assistant.anomaly import AnomalyDetector
from assistant.config import DEFAULT_CONFIG


class _CollectingTemporaryDirectory(tempfile.TemporaryDirectory):
    """Collect transient sqlite connection cycles before Windows removes the fixture."""

    def cleanup(self):
        gc.collect()
        super().cleanup()


class FakeEngine:
    def __init__(self, state):
        self.state = state

    def current(self, refresh=False):
        return dict(self.state)


class FakeIntelligence:
    def __init__(self, activity="browsing"):
        self.activity = activity

    def snapshot(self, refresh=False):
        return {
            "activity": {
                "activity": self.activity,
                "label": self.activity,
                "confidence": 0.9,
            }
        }


class SequenceProcesses:
    def __init__(self, rows):
        self.rows = list(rows)
        self.index = 0

    def __call__(self):
        row = self.rows[min(self.index, len(self.rows) - 1)]
        self.index += 1
        return [dict(x) for x in row]


class AnomalyDetectionTests(unittest.TestCase):
    def _state(self, cpu=20, memory=40, process="msedge.exe", app_kind="browser"):
        return {
            "external": {"process": process, "app_kind": app_kind},
            "system": {"cpu_percent": cpu, "memory_percent": memory, "nova_memory_mb": 80},
        }

    def test_learns_baseline_then_emits_sustained_system_anomaly(self):
        with _CollectingTemporaryDirectory() as td:
            engine = FakeEngine(self._state())
            detector = AnomalyDetector(
                engine,
                FakeIntelligence("browsing"),
                config={
                    "baseline_min_samples": 3,
                    "sustained_samples": 2,
                    "system_cpu_floor": 60,
                    "system_cpu_min_delta": 15,
                    "event_cooldown_seconds": 10,
                },
                db_path=Path(td) / "anomaly.db",
                process_sensor=lambda: [],
            )
            for _ in range(3):
                detector.sample_once()
            status = detector.status()
            self.assertTrue(status["baseline_ready"])
            self.assertEqual(status["baseline_samples"], 3)

            engine.state = self._state(cpu=91, memory=40)
            first = detector.sample_once()
            second = detector.sample_once()
            self.assertFalse(any(x["event_type"] == "system_cpu_anomaly" for x in first["events"]))
            self.assertTrue(any(x["event_type"] == "system_cpu_anomaly" for x in second["events"]))

    def test_gaming_context_treats_high_cpu_as_expected_until_extreme(self):
        with _CollectingTemporaryDirectory() as td:
            engine = FakeEngine(self._state(cpu=70, memory=55, process="javaw.exe", app_kind="game"))
            detector = AnomalyDetector(
                engine,
                FakeIntelligence("gaming"),
                config={"baseline_min_samples": 3, "sustained_samples": 1, "system_cpu_floor": 60},
                db_path=Path(td) / "anomaly.db",
                process_sensor=lambda: [{"pid": 1, "name": "javaw.exe", "cpu_percent": 50, "memory_percent": 8}],
            )
            for _ in range(3):
                detector.sample_once()
            engine.state = self._state(cpu=95, memory=60, process="javaw.exe", app_kind="game")
            result = detector.sample_once()
            self.assertFalse(any(x["event_type"] == "system_cpu_anomaly" for x in result["events"]))

    def test_new_heavy_process_is_detected_and_user_can_mark_expected(self):
        with _CollectingTemporaryDirectory() as td:
            engine = FakeEngine(self._state())
            normal = [{"pid": 10, "name": "msedge.exe", "cpu_percent": 3, "memory_percent": 4}]
            heavy = normal + [{"pid": 20, "name": "mystery.exe", "cpu_percent": 60, "memory_percent": 11}]
            detector = AnomalyDetector(
                engine,
                FakeIntelligence("browsing"),
                config={"baseline_min_samples": 3, "process_baseline_min_samples": 3, "sustained_samples": 2},
                db_path=Path(td) / "anomaly.db",
                process_sensor=SequenceProcesses([normal, normal, normal, heavy, heavy]),
            )
            for _ in range(3):
                detector.sample_once()
            detector.sample_once()
            result = detector.sample_once()
            events = [x for x in result["events"] if x["event_type"] == "process_resource_anomaly"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["process_name"], "mystery.exe")

            detector.acknowledge()
            detector.mark_process_expected("mystery.exe", True)
            detector._streaks.clear()
            detector._last_emit_at.clear()
            detector.process_sensor = lambda: heavy
            self.assertFalse(any(x["event_type"] == "process_resource_anomaly" for x in detector.sample_once()["events"]))

    def test_crash_signal_escalates_when_repeated(self):
        with _CollectingTemporaryDirectory() as td:
            engine = FakeEngine(self._state())
            detector = AnomalyDetector(
                engine,
                FakeIntelligence("browsing"),
                config={"baseline_min_samples": 3, "event_cooldown_seconds": 0},
                db_path=Path(td) / "anomaly.db",
                process_sensor=lambda: [],
            )
            first = detector._crash_signals([{"pid": 100, "name": "WerFault.exe", "cpu_percent": 0, "memory_percent": 0}], "browsing")
            detector._last_emit_at.clear()
            second = detector._crash_signals([{"pid": 101, "name": "WerFault.exe", "cpu_percent": 0, "memory_percent": 0}], "browsing")
            self.assertEqual(first[0]["severity"], "warn")
            self.assertEqual(second[0]["severity"], "high")

    def test_privacy_status_routing_and_defaults(self):
        with _CollectingTemporaryDirectory() as td:
            detector = AnomalyDetector(
                FakeEngine(self._state()),
                FakeIntelligence(),
                db_path=Path(td) / "anomaly.db",
                process_sensor=lambda: [],
            )
            status = detector.status()
            self.assertFalse(status["uses_llm"])
            self.assertFalse(status["captures_screen"])
            self.assertFalse(status["captures_keyboard"])
            self.assertFalse(status["reads_clipboard"])
            self.assertFalse(status["reads_cmdline"])
            self.assertFalse(status["auto_remediation"])
            self.assertTrue(DEFAULT_CONFIG["anomaly_detection"]["enabled"])
            self.assertEqual(anomaly_direct_intent("¿Hay algo raro en mi PC?"), "recent")
            self.assertEqual(anomaly_direct_intent("¿Estado del detector de anomalías?"), "status")
            self.assertIsNone(anomaly_direct_intent("abre el administrador de tareas"))


if __name__ == "__main__":
    unittest.main()
