from __future__ import annotations

import unittest

from assistant.ui_action_approval import ActionApprovalController, attach_action_approval


class _Broker:
    def __init__(self): self.handler = None; self.calls = []
    def set_approval_handler(self, handler): self.handler = handler
    def approve(self, request_id, mode="once"): self.calls.append(("approve", request_id, mode)); return True
    def deny(self, request_id, reason="user"): self.calls.append(("deny", request_id, reason)); return True
    def cancel_all(self, reason, shutdown=False): self.calls.append(("cancel", reason, shutdown)); return 1


class _Window:
    def __init__(self): self.destroyed = False
    def destroy(self): self.destroyed = True


class UIActionApprovalTests(unittest.TestCase):
    def test_only_local_controller_installs_handler_and_close_denies(self):
        broker = _Broker()
        ui = type("UI", (), {})()
        ui.root = object()
        ui.agent = type("Agent", (), {"tools": type("Tools", (), {"action_broker": broker})()})()
        controller = attach_action_approval(ui)
        self.assertIs(broker.handler.__self__, controller)
        window = _Window()
        controller._windows["r1"] = window
        controller._decide("r1", "deny")
        self.assertEqual(broker.calls[-1][:2], ("deny", "r1"))
        self.assertTrue(window.destroyed)

    def test_allow_task_and_cancel_release_windows(self):
        controller = object.__new__(ActionApprovalController)
        controller.broker = _Broker()
        controller._windows = {"r2": _Window(), "r3": _Window()}
        controller._decide("r2", "task")
        self.assertEqual(controller.broker.calls[-1], ("approve", "r2", "task"))
        controller.cancel_all("shutdown", shutdown=True)
        self.assertEqual(controller._windows, {})


if __name__ == "__main__":
    unittest.main()
