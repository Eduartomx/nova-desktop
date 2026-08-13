from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assistant.agent import LocalAgent
from assistant.agent_diagnostics import performance_direct_intent, performance_window
from assistant.llm_benchmark import format_llm_benchmark, run_llm_benchmark
from assistant.llm_performance import LLMPerformanceMonitor
from assistant.memory import MemoryStore
from assistant.profiler import PerformanceProfiler


class LLMPerformanceMonitorTests(unittest.TestCase):
    def make_monitor(self, td):
        return LLMPerformanceMonitor(
            Path(td) / "llm_performance.db",
            {
                "enabled": True,
                "gpu_sampling": False,
                "max_events": 200,
                "cold_start_ms": 500,
                "slow_response_ms": 1000,
            },
        )

    def test_ollama_metrics_are_converted_and_summarized(self):
        with tempfile.TemporaryDirectory() as td:
            monitor = self.make_monitor(td)
            row = monitor.record_success(
                model="qwen-test:4b",
                label="normal",
                wall_ms=4100,
                response={
                    "total_duration": 4_000_000_000,
                    "load_duration": 600_000_000,
                    "prompt_eval_count": 200,
                    "prompt_eval_duration": 400_000_000,
                    "eval_count": 90,
                    "eval_duration": 3_000_000_000,
                    "done_reason": "stop",
                },
                context={"message_count": 5, "tool_count": 3, "prompt_chars": 2400, "system_chars": 900, "history_messages": 3},
                gpu_before={"utilization": 20, "vram_used_mb": 6000, "vram_total_mb": 8000},
                gpu_after={"utilization": 70, "vram_used_mb": 7000, "vram_total_mb": 8000},
            )
            self.assertAlmostEqual(row["server_total_ms"], 4000.0)
            self.assertAlmostEqual(row["load_ms"], 600.0)
            self.assertAlmostEqual(row["prompt_tps"], 500.0)
            self.assertAlmostEqual(row["eval_tps"], 30.0)

            report = monitor.summary(hours=24, session_only=True)
            self.assertEqual(report["calls"], 1)
            self.assertEqual(report["avg_prompt_tokens"], 200.0)
            self.assertEqual(report["avg_output_tokens"], 90.0)
            self.assertIn("cold_start", report["cause_codes"])
            self.assertIn("generation_heavy", report["cause_codes"])
            self.assertGreaterEqual(report["max_vram_percent"], 87.0)
            self.assertIn("gpu_memory_pressure", report["cause_codes"])

    def test_db_schema_contains_no_prompt_or_response_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "llm_performance.db"
            monitor = LLMPerformanceMonitor(path, {"gpu_sampling": False})
            monitor.record_failure(
                model="qwen-test",
                label="failure",
                wall_ms=1234,
                context={"message_count": 2, "tool_count": 0, "prompt_chars": 999, "system_chars": 300, "history_messages": 0},
                gpu_before=None,
                gpu_after=None,
                error_type="Timeout",
            )
            with sqlite3.connect(path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
                row = conn.execute("SELECT * FROM llm_calls LIMIT 1").fetchone()
            forbidden = {"prompt", "response", "content", "messages", "tool_arguments", "secret", "api_key"}
            self.assertTrue(forbidden.isdisjoint(columns))
            self.assertIsNotNone(row)

    def test_context_metrics_store_counts_not_text(self):
        metrics = LLMPerformanceMonitor.context_metrics(
            [
                {"role": "system", "content": "abc"},
                {"role": "user", "content": "12345"},
                {"role": "assistant", "content": "xy"},
            ],
            [{"type": "function"}, {"type": "function"}],
        )
        self.assertEqual(metrics["message_count"], 3)
        self.assertEqual(metrics["tool_count"], 2)
        self.assertEqual(metrics["prompt_chars"], 10)
        self.assertEqual(metrics["system_chars"], 3)
        self.assertNotIn("content", metrics)


class AgentInstrumentationTests(unittest.TestCase):
    def test_agent_captures_ollama_usage_without_persisting_response_text(self):
        with tempfile.TemporaryDirectory() as td:
            memory = MemoryStore(Path(td) / "nova.db")
            monitor = LLMPerformanceMonitor(Path(td) / "llm.db", {"gpu_sampling": False})
            agent = LocalAgent(
                {"model": "qwen-test", "local_llm": {"timeout_seconds": 9}},
                memory=memory,
            )
            agent.llm_performance = monitor
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "message": {"content": "SECRETO-RESPUESTA"},
                "total_duration": 1_500_000_000,
                "load_duration": 100_000_000,
                "prompt_eval_count": 50,
                "prompt_eval_duration": 200_000_000,
                "eval_count": 30,
                "eval_duration": 1_000_000_000,
                "done_reason": "stop",
            }
            with mock.patch("assistant.agent.requests.post", return_value=response) as post:
                data = agent._ollama_chat(
                    [{"role": "user", "content": "SECRETO-PROMPT"}],
                    performance_label="unit",
                )
            self.assertEqual(data["message"]["content"], "SECRETO-RESPUESTA")
            self.assertEqual(post.call_args.kwargs["timeout"], 9.0)
            self.assertEqual(agent._last_llm_metrics["prompt_tokens"], 50)
            self.assertEqual(agent._last_llm_metrics["output_tokens"], 30)
            with sqlite3.connect(Path(td) / "llm.db") as conn:
                blob = " ".join(str(x) for x in conn.execute("SELECT * FROM llm_calls").fetchone())
            self.assertNotIn("SECRETO-PROMPT", blob)
            self.assertNotIn("SECRETO-RESPUESTA", blob)


class SessionWindowTests(unittest.TestCase):
    def test_profiler_session_does_not_mix_previous_process_events(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "performance.db"
            first = PerformanceProfiler(path, {"slow_ms": 10})
            first.record("agent.total", 100.0)
            second = PerformanceProfiler(path, {"slow_ms": 10})
            second.record("agent.total", 300.0)
            session = second.summary(hours=720, session_only=True)
            all_rows = second.summary(hours=24, session_only=False)
            self.assertEqual(session["operations"][0]["calls"], 1)
            self.assertEqual(float(session["operations"][0]["avg_ms"]), 300.0)
            self.assertEqual(all_rows["operations"][0]["calls"], 2)


class BenchmarkRoutingTests(unittest.TestCase):
    def test_routing_and_windows(self):
        self.assertEqual(performance_direct_intent("Nova, prueba tu rendimiento"), "benchmark")
        self.assertEqual(performance_direct_intent("¿Por qué Ollama tarda tanto?"), "llm_performance")
        self.assertEqual(performance_window("rendimiento de esta sesión")[1], True)
        self.assertEqual(performance_window("rendimiento de los últimos 15 minutos")[0], 0.25)
        self.assertEqual(performance_window("rendimiento de la última hora")[0], 1.0)
        self.assertEqual(performance_window("rendimiento de las últimas 24 horas")[0], 24.0)

    def test_benchmark_is_bounded_and_uses_three_labels(self):
        class FakeAgent:
            model = "fake:4b"
            config = {"llm_performance": {"benchmark_max_tokens": 40}}

            def __init__(self):
                self._last_llm_metrics = {}
                self.calls = []

            def _ollama_chat(self, messages, tools=None, options_override=None, performance_label=""):
                self.calls.append((performance_label, dict(options_override or {}), len(tools or [])))
                self._last_llm_metrics = {
                    "success": True,
                    "wall_ms": 100,
                    "load_ms": 1,
                    "prompt_eval_ms": 20,
                    "eval_ms": 70,
                    "eval_tps": 40,
                }
                return {"message": {"content": "ignored"}}

        agent = FakeAgent()
        report = run_llm_benchmark(agent)
        self.assertTrue(report["ok"])
        self.assertEqual([x[0] for x in agent.calls], ["benchmark.fast", "benchmark.normal", "benchmark.tools"])
        self.assertTrue(all(call[1].get("num_predict", 999) <= 40 for call in agent.calls[1:]))
        text = format_llm_benchmark(report)
        self.assertIn("Nova LLM Benchmark", text)
        self.assertIn("Respuesta corta", text)


if __name__ == "__main__":
    unittest.main()
