from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from assistant.autostart import AutostartManager
from assistant.instance_commands import InstanceCommandMailbox
from assistant.instance_lock import runtime_identity, runtime_paths
from assistant.tray_controller import TrayController


class Registry:
    def __init__(self): self.values = {}
    def read(self, name): return self.values.get(name)
    def write(self, name, value): self.values[name] = value
    def delete(self, name): self.values.pop(name, None)


class AutostartOwnershipTests(unittest.TestCase):
    def test_foreign_installation_entry_is_not_removed(self):
        with tempfile.TemporaryDirectory(prefix="Nova Current ") as td:
            root = Path(td)
            (root / "app.py").write_text("", encoding="utf-8")
            backend = Registry()
            backend.values["NovaDesktop"] = '"C:\\Other Nova\\pythonw.exe" "C:\\Other Nova\\app.py" --background'
            manager = AutostartManager(root, backend=backend)
            before = backend.values["NovaDesktop"]
            result = manager.configure(False)
            self.assertFalse(result["ok"])
            self.assertTrue(result["conflict"])
            self.assertEqual(result["error"], "entry_belongs_to_other_installation")
            self.assertEqual(backend.values["NovaDesktop"], before)
            self.assertTrue(manager.status()["conflict"])


class MailboxGenerationTests(unittest.TestCase):
    def test_old_shutdown_command_never_reaches_new_owner(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "commands"
            old_owner = "a" * 32
            new_owner = "b" * 32
            old = InstanceCommandMailbox(directory, owner_id=old_owner)
            self.assertTrue(old.send("shutdown_for_update", target_owner_id=old_owner))
            new = InstanceCommandMailbox(directory, owner_id=new_owner)
            result = new.consume(owner_id=new_owner)
            self.assertIsNotNone(result)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "wrong_owner")
            self.assertIsNone(new.consume(owner_id=new_owner))

    def test_command_targeted_to_other_owner_is_rejected_and_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "commands"
            sender = InstanceCommandMailbox(directory, owner_id="a" * 32)
            self.assertTrue(sender.send("show", target_owner_id="a" * 32))
            receiver = InstanceCommandMailbox(directory, owner_id="c" * 32)
            result = receiver.consume(owner_id="c" * 32)
            self.assertEqual(result, {"ok": False, "error": "wrong_owner"})
            self.assertEqual(list(directory.glob("*.json")), [])


class ScopeTests(unittest.TestCase):
    def test_lock_owner_and_commands_share_same_user_session_scope(self):
        identity = runtime_identity()
        paths = runtime_paths()
        self.assertEqual(paths.scope_id, identity["scope_id"])
        self.assertEqual(paths.lock.parent, paths.owner.parent)
        self.assertEqual(paths.commands.parent, paths.lock.parent)
        self.assertEqual(paths.directory.name, "scope-" + identity["scope_id"])
        self.assertNotIn("\\", identity["user_hash"])
        self.assertNotIn("S-1-", identity["user_hash"])


class _Lifecycle:
    def show_window(self): return True
    def request_shutdown(self, _reason): return True


class _UI:
    config = {}
    agent = None


class AsyncReadyIcon:
    visible = True
    def __init__(self): self.stop_calls = 0
    def run_detached(self, setup=None):
        def ready():
            if setup is not None: setup(self)
        threading.Thread(target=ready, daemon=True).start()
    def stop(self): self.stop_calls += 1
    def update_menu(self): pass
    def notify(self, *_args): pass


class AsyncFailIcon(AsyncReadyIcon):
    def run_detached(self, setup=None):
        raise RuntimeError("backend failed")


class AsyncTimeoutIcon(AsyncReadyIcon):
    def run_detached(self, setup=None):
        return None


class TrayReadinessTests(unittest.TestCase):
    def make(self, icon, timeout=0.2):
        return TrayController(_UI(), _Lifecycle(), icon_factory=lambda _tray: icon, ready_timeout=timeout)

    def test_async_backend_success_marks_available_only_after_setup(self):
        icon = AsyncReadyIcon()
        tray = self.make(icon)
        self.assertTrue(tray.start())
        self.assertTrue(tray.available)
        self.assertFalse(tray.degraded)
        self.assertTrue(tray.status()["ready"])

    def test_async_backend_failure_is_degraded(self):
        tray = self.make(AsyncFailIcon())
        self.assertFalse(tray.start())
        self.assertFalse(tray.available)
        self.assertTrue(tray.degraded)

    def test_async_backend_timeout_is_degraded(self):
        tray = self.make(AsyncTimeoutIcon(), timeout=0.1)
        self.assertFalse(tray.start())
        self.assertFalse(tray.available)
        self.assertTrue(tray.degraded)
        self.assertIn("timeout", tray.last_error)


class SecondaryLaunchTests(unittest.TestCase):
    def test_show_delivery_failure_returns_nonzero_secondary_code(self):
        import app

        class FakeLock:
            def __init__(self, *args, **kwargs): pass
            def acquire(self): return False
            def read_owner(self): return {"pid": 77, "owner_id": "a" * 32}

        class FakeMailbox:
            def __init__(self, *args, **kwargs): pass
            def send(self, *args, **kwargs): return False

        fake_paths = SimpleNamespace(lock=Path("x.lock"), owner=Path("owner.json"), commands=Path("commands"))
        with mock.patch("assistant.instance_lock.InstanceLock", FakeLock), \
             mock.patch("assistant.instance_lock.runtime_paths", return_value=fake_paths), \
             mock.patch("assistant.instance_commands.InstanceCommandMailbox", FakeMailbox):
            lock, _mailbox, code = app._claim_instance()
        self.assertIsNone(lock)
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
