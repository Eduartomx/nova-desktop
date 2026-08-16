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
        "target": "demo.txt", "explicit_intent": True, "observations": {"stable": True},
    }
    values.update(overrides)
    return ActionContext(**values)


class ActionBrokerTests(unittest.TestCase):
    def broker(self, profile="balanced", names=None, audit=None, clock=None):
        kwargs = {"tool_names": names or {"read_file", "clipboard_read", "write_file", "powershell", "browser_press"}, "audit_path": audit}
        if clock is not None:
            kwargs["clock"] = clock
        return ActionBroker({"security": {"profile": profile, "approval_timeout_seconds": 2}}, **kwargs)

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
        pending = expiring.request("write_file", args, context(args=args), timeout=1)
        now[0] = 12.0
        self.assertFalse(expiring.approve(pending["request"]["request_id"]))

        cancelling = self.broker()
        pending = cancelling.request("write_file", args, context(args=args))
        self.assertEqual(cancelling.cancel_all("shutdown", shutdown=True), 1)
        self.assertFalse(cancelling.approve(pending["request"]["request_id"]))

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
