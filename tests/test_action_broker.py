from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from assistant.action_broker import ActionBroker
from assistant.action_context import ActionContext, arguments_hash, build_action_context


def context(tool="write_file", args=None, **overrides):
    args = dict(args or {"path": "demo.txt", "content": "secret-value"})
    values = {
        "tool": tool, "arguments_sha256": arguments_hash(tool, args), "owner_id": "owner-a",
        "scope": "scope-a", "session_id": "session-a", "task_id": "task-a",
        "target": "demo.txt", "explicit_intent": True, "intent_source": "local_user",
        "intent_sha256": "a" * 64, "intent_session_id": "session-a",
        "intent_request_id": "b" * 32, "observations": {"stable": True},
    }
    values.update(overrides)
    return ActionContext(**values)


class ActionBrokerTests(unittest.TestCase):
    def broker(self, profile="balanced", names=None, audit=None, clock=None, security=None):
        settings = {"profile": profile, "approval_timeout_seconds": 2}
        settings.update(security or {})
        kwargs = {"tool_names": names or {"read_file", "clipboard_read", "write_file", "powershell", "browser_press"}, "audit_path": audit}
        if clock is not None:
            kwargs["clock"] = clock
        return ActionBroker({"security": settings}, **kwargs)

    def test_read_only_and_sensitive_read_intent(self):
        broker = self.broker()
        ran = []
        read_args = {"path": "x"}
        result = broker.execute("read_file", read_args, context("read_file", read_args), lambda: ran.append(1) or {"ok": True})
        self.assertTrue(result["ok"])
        self.assertEqual(ran, [1])
        clip = context("clipboard_read", {}, explicit_intent=False)
        denied = broker.execute("clipboard_read", {}, clip, lambda: self.fail("must not run"))
        self.assertEqual(denied["error"], "explicit_intent_required")
        forged = context("clipboard_read", {}, intent_source="planner")
        denied = broker.execute("clipboard_read", {}, forged, lambda: self.fail("must not run"))
        self.assertEqual(denied["error"], "explicit_intent_required")
        for profile in ("balanced", "trusted"):
            allowed = self.broker(profile).execute(
                "clipboard_read", {}, context("clipboard_read", {}, explicit_intent=True), lambda: {"ok": True},
            )
            self.assertTrue(allowed["ok"], profile)
        safe = self.broker("safe")
        safe.set_approval_handler(lambda row: safe.approve(row["request_id"]))
        self.assertTrue(safe.execute(
            "clipboard_read", {}, context("clipboard_read", {}, explicit_intent=True), lambda: {"ok": True},
        )["ok"])

    def test_allow_once_executes_exactly_once_and_double_approval_fails(self):
        broker = self.broker()
        seen = []
        broker.set_approval_handler(lambda row: (seen.append(row), broker.approve(row["request_id"], mode="once")))
        calls = []
        args = {"path": "demo.txt", "content": "hidden"}
        result = broker.execute("write_file", args, context(args=args), lambda: calls.append("run") or {"ok": True})
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["run"])
        self.assertFalse(broker.approve(seen[0]["request_id"]))

    def test_task_grant_is_bounded_and_high_risk_never_gets_it(self):
        broker = self.broker()
        prompts = []
        broker.set_approval_handler(lambda row: (prompts.append(row), broker.approve(row["request_id"], mode="task")))
        args = {"path": "a", "content": "b"}
        broker.execute("write_file", args, context(args=args), lambda: {"ok": True})
        broker.execute("write_file", args, context(args=args), lambda: {"ok": True})
        self.assertEqual(len(prompts), 1)

        other = context(args=args, task_id="task-b")
        broker.set_approval_handler(lambda row: (prompts.append(row), broker.deny(row["request_id"])))
        denied = broker.execute("write_file", args, other, lambda: self.fail("must not run"))
        self.assertFalse(denied["ok"])
        self.assertEqual(len(prompts), 2)

        other_session = context(args=args, session_id="session-b")
        denied = broker.execute("write_file", args, other_session, lambda: self.fail("must not run"))
        self.assertFalse(denied["ok"])
        self.assertEqual(len(prompts), 3)

        high_args = {"key": "Enter"}
        high = context("browser_press", high_args)
        modes = []
        def high_handler(row):
            modes.append(row["allow_task_grant"])
            self.assertFalse(broker.approve(row["request_id"], mode="task"))
            broker.approve(row["request_id"], mode="once")
        broker.set_approval_handler(high_handler)
        broker.execute("browser_press", high_args, high, lambda: {"ok": True})
        broker.execute("browser_press", high_args, high, lambda: {"ok": True})
        self.assertEqual(modes, [False, False])

    def test_denial_expiration_cancellation_and_unknown_fail_closed(self):
        broker = self.broker()
        broker.set_approval_handler(lambda row: broker.deny(row["request_id"]))
        args = {"path": "x", "content": "y"}
        self.assertEqual(broker.execute("write_file", args, context(args=args), lambda: None)["authorization_state"], "denied")
        unknown = broker.execute("invented_tool", {}, context("invented_tool", {}), lambda: self.fail("must not run"))
        self.assertEqual(unknown["error"], "forbidden_action")

        now = [10.0]
        expiring = self.broker(clock=lambda: now[0])
        expiring.set_approval_handler(lambda row: (now.__setitem__(0, 12.0), expiring.approve(row["request_id"])))
        expired = expiring.request("write_file", args, context(args=args), timeout=1)
        self.assertEqual(expired["authorization_state"], "expired")

        cancelling = self.broker()
        shown = threading.Event()
        rows = []
        cancelling.set_approval_handler(lambda row: (rows.append(row), shown.set()))
        worker = threading.Thread(target=lambda: cancelling.request("write_file", args, context(args=args)), daemon=True)
        worker.start()
        self.assertTrue(shown.wait(1))
        self.assertEqual(cancelling.cancel_all("shutdown", shutdown=True), 1)
        worker.join(1)
        self.assertFalse(cancelling.approve(rows[0]["request_id"]))

    def test_approval_expires_before_consume_and_double_consume_fails(self):
        now = [1.0]
        broker = self.broker(clock=lambda: now[0])
        broker.set_approval_handler(lambda row: broker.approve(row["request_id"]))
        args = {"path": "x", "content": "y"}
        ctx = context(args=args)
        decision = broker.request("write_file", args, ctx, timeout=2)
        now[0] = 4.0
        self.assertEqual(broker.consume(decision["request_id"], ctx)["error"], "authorization_expired")

        now[0] = 10.0
        decision = broker.request("write_file", args, ctx, timeout=2)
        self.assertTrue(broker.consume(decision["request_id"], ctx)["ok"])
        self.assertEqual(broker.consume(decision["request_id"], ctx)["error"], "authorization_already_consumed")

    def test_abandoned_pending_and_approved_requests_expire_on_sweep(self):
        now = [100.0]
        broker = self.broker(clock=lambda: now[0], security={"action_active_limit": 1})
        args = {"path": "pending", "content": "x"}
        shown = threading.Event()
        rows = []
        broker.set_approval_handler(lambda row: (rows.append(row), shown.set()))
        results = []
        worker = threading.Thread(target=lambda: results.append(broker.request("write_file", args, context(args=args), timeout=2)), daemon=True)
        worker.start()
        self.assertTrue(shown.wait(1))
        now[0] = 103.0
        self.assertEqual(broker.pending(), [])
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["authorization_state"], "expired")

        now[0] = 200.0
        broker.set_approval_handler(lambda row: broker.approve(row["request_id"]))
        decision = broker.request("write_file", args, context(args=args), timeout=2)
        self.assertEqual(broker.request_state(decision["request_id"]), "approved")
        now[0] = 203.0
        self.assertEqual(broker.request_state(decision["request_id"]), "expired")
        self.assertEqual(broker.consume(decision["request_id"], context(args=args))["error"], "authorization_expired")

    def test_expired_task_approval_never_creates_a_grant(self):
        now = [10.0]
        broker = self.broker(clock=lambda: now[0])
        prompts = []
        broker.set_approval_handler(lambda row: (prompts.append(row), broker.approve(row["request_id"], mode="task")))
        args = {"path": "grant", "content": "x"}
        ctx = context(args=args)
        decision = broker.request("write_file", args, ctx, timeout=2)
        now[0] = 13.0
        self.assertEqual(broker.consume(decision["request_id"], ctx)["error"], "authorization_expired")
        self.assertEqual(broker._task_grants, set())
        now[0] = 20.0
        broker.set_approval_handler(lambda row: (prompts.append(row), broker.deny(row["request_id"])))
        denied = broker.request("write_file", args, ctx, timeout=2)
        self.assertEqual(denied["authorization_state"], "denied")
        self.assertEqual(len(prompts), 2)

    def test_capacity_check_and_insert_are_atomic_and_expiry_recovers_capacity(self):
        broker = self.broker(security={"action_active_limit": 2, "approval_timeout_seconds": 5})
        shown = threading.Event()
        rows = []
        rows_lock = threading.Lock()
        maximum = [0]
        def handler(row):
            with rows_lock:
                rows.append(row)
                maximum[0] = max(maximum[0], len(broker.pending()))
                if len(rows) >= 2:
                    shown.set()
        broker.set_approval_handler(handler)
        results = []
        def request(index):
            args = {"path": f"row-{index}", "content": "x"}
            results.append(broker.request("write_file", args, context(args=args), timeout=5))
        workers = [threading.Thread(target=request, args=(index,), daemon=True) for index in range(8)]
        for worker in workers:
            worker.start()
        self.assertTrue(shown.wait(2))
        self.assertEqual(len(broker.pending()), 2)
        broker.cancel_all("capacity_test")
        for worker in workers:
            worker.join(2)
        self.assertEqual(len(rows), 2)
        self.assertLessEqual(maximum[0], 2)
        self.assertEqual(sum(row.get("error") == "authorization_capacity_exceeded" for row in results), 6)

        broker = self.broker(security={"action_active_limit": 1})
        first_shown = threading.Event()
        broker.set_approval_handler(lambda _row: first_shown.set())
        args = {"path": "first", "content": "x"}
        worker = threading.Thread(target=lambda: broker.request("write_file", args, context(args=args), timeout=1), daemon=True)
        worker.start()
        self.assertTrue(first_shown.wait(1))
        broker.cancel_all("release_capacity")
        worker.join(1)
        broker.set_approval_handler(lambda row: broker.deny(row["request_id"]))
        args2 = {"path": "second", "content": "x"}
        self.assertEqual(broker.request("write_file", args2, context(args=args2))["authorization_state"], "denied")

    def test_context_changes_reject_owner_scope_task_hash_and_file_toctou(self):
        variants = {
            "owner": {"owner_id": "owner-b"}, "scope": {"scope": "scope-b"},
            "task": {"task_id": "task-b"}, "hash": {"arguments_sha256": "0" * 64},
            "window": {"observations": {"window": "other"}},
            "control": {"observations": {"control": "other"}},
            "url": {"observations": {"browser_url": "https://other.test/"}},
            "form": {"observations": {"browser_control": {"formaction": "/changed"}}},
        }
        args = {"path": "x", "content": "y"}
        for label, changed in variants.items():
            with self.subTest(label=label):
                broker = self.broker()
                broker.set_approval_handler(lambda row, b=broker: b.approve(row["request_id"]))
                initial = context(args=args)
                current = context(args=args, **changed)
                result = broker.execute("write_file", args, initial, lambda: self.fail("must not run"), context_provider=lambda c=current: c)
                self.assertEqual(result["error"], "authorization_context_changed")

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "file.txt"
            target.write_text("before", encoding="utf-8")
            class Tools:
                action_owner_id = "owner-a"; action_scope = "scope-a"; action_session_id = "session-a"; action_task_id = "task-a"
                def _resolve_path(self, value): return Path(value)
                def _ensure_allowed(self, value): return Path(value)
            fake = Tools()
            file_args = {"path": str(target), "content": "new"}
            initial = build_action_context("write_file", file_args, tools=fake, user_text="escribe")
            broker = self.broker()
            def approve_after_change(row):
                target.write_text("changed", encoding="utf-8")
                broker.approve(row["request_id"])
            broker.set_approval_handler(approve_after_change)
            result = broker.execute("write_file", file_args, initial, lambda: self.fail("must not run"), context_provider=lambda: build_action_context("write_file", file_args, tools=fake, user_text="escribe"))
            self.assertEqual(result["error"], "authorization_context_changed")

    def test_audit_contains_hashes_but_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "audit.jsonl"
            broker = self.broker(audit=audit)
            broker.set_approval_handler(lambda row: broker.approve(row["request_id"]))
            args = {"path": "demo.txt", "content": "TOP-SECRET"}
            broker.execute("write_file", args, context(args=args), lambda: {"ok": True})
            raw = audit.read_text(encoding="utf-8")
            self.assertNotIn("TOP-SECRET", raw)
            self.assertNotIn("content", raw)
            for line in raw.splitlines():
                self.assertEqual(len(json.loads(line)["arguments_sha256"]), 64)

    def test_audit_rotation_is_bounded_valid_and_secret_free(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "action_audit.jsonl"
            broker = self.broker(
                audit=audit,
                security={"action_audit_max_bytes": 4096, "action_audit_rotations": 2},
            )
            broker.set_approval_handler(lambda row: broker.approve(row["request_id"]))
            for index in range(30):
                args = {"path": f"demo-{index}.txt", "content": "ROTATION-TOP-SECRET"}
                broker.execute("write_file", args, context(args=args), lambda: {"ok": True})
            files = sorted(Path(td).glob("action_audit.jsonl*"))
            self.assertLessEqual(len(files), 3)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 4096 * 3)
            for path in files:
                raw = path.read_text(encoding="utf-8")
                self.assertNotIn("ROTATION-TOP-SECRET", raw)
                for line in raw.splitlines():
                    json.loads(line)

    def test_pruning_preserves_active_and_bounds_terminal_history(self):
        broker = self.broker(security={"action_history_limit": 16})
        active_seen = threading.Event()
        active_rows = []
        broker.set_approval_handler(lambda row: (active_rows.append(row), active_seen.set()))
        args = {"path": "active", "content": "x"}
        worker = threading.Thread(target=lambda: broker.request("write_file", args, context(args=args)), daemon=True)
        worker.start()
        self.assertTrue(active_seen.wait(1))

        broker.set_approval_handler(lambda row: broker.deny(row["request_id"]))
        for index in range(30):
            row_args = {"path": str(index), "content": "x"}
            broker.request("write_file", row_args, context(args=row_args))
        broker.prune()
        self.assertIn(active_rows[0]["request_id"], {row["request_id"] for row in broker.pending()})
        self.assertLessEqual(sum(1 for row in broker._pending.values() if row.get("state") != "pending"), 16)
        broker.cancel_all("test")
        worker.join(1)

    def test_racing_terminal_decisions_never_execute_twice(self):
        broker = self.broker()
        shown = threading.Event()
        rows = []
        calls = []
        broker.set_approval_handler(lambda row: (rows.append(row), shown.set()))
        args = {"path": "race", "content": "secret"}
        worker = threading.Thread(
            target=lambda: broker.execute("write_file", args, context(args=args), lambda: calls.append(1) or {"ok": True}),
            daemon=True,
        )
        worker.start()
        self.assertTrue(shown.wait(1))
        request_id = rows[0]["request_id"]
        racers = [
            threading.Thread(target=lambda: broker.approve(request_id)),
            threading.Thread(target=lambda: broker.deny(request_id)),
            threading.Thread(target=lambda: broker.cancel_all("race")),
        ]
        for row in racers: row.start()
        for row in racers: row.join()
        worker.join(1)
        self.assertLessEqual(len(calls), 1)

    def test_shutdown_cancellation_releases_waiting_worker(self):
        broker = self.broker()
        shown = threading.Event()
        broker.set_approval_handler(lambda _row: shown.set())
        args = {"path": "demo.txt", "content": "hidden"}
        results = []
        worker = threading.Thread(
            target=lambda: results.append(broker.execute("write_file", args, context(args=args), lambda: self.fail("must not run"))),
            daemon=True,
        )
        worker.start()
        self.assertTrue(shown.wait(1.0))
        self.assertEqual(broker.cancel_all("shutdown", shutdown=True), 1)
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["authorization_state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
