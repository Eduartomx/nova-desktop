from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from assistant.autostart import AutostartManager
from assistant.instance_commands import InstanceCommandMailbox
from assistant.instance_lock import InstanceLock
from assistant.runtime_lifecycle import RuntimeLifecycleManager
from assistant.tray_controller import TrayController


TK_WIDGETS = {
    "Frame", "Label", "Button", "Text", "Entry", "Listbox", "Canvas",
    "Checkbutton", "Radiobutton", "Scale", "Scrollbar", "Spinbox",
}


class TkPaddingSafetyTests(unittest.TestCase):
    def test_widget_constructor_padding_is_never_a_tuple(self):
        assistant_dir = Path(__file__).resolve().parents[1] / "nova" / "assistant"
        offenders: list[str] = []
        for path in sorted(assistant_dir.glob("ui*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "tk"
                    and func.attr in TK_WIDGETS
                ):
                    continue
                for keyword in node.keywords:
                    if keyword.arg in {"padx", "pady"} and isinstance(keyword.value, (ast.Tuple, ast.List)):
                        offenders.append(f"{path.name}:{node.lineno}:{func.attr}:{keyword.arg}")
        self.assertEqual(offenders, [], "Padding asimétrico debe ir en pack/grid, no en el constructor Tk")


class _Root:
    def __init__(self): self.hidden = False; self.destroy_calls = 0
    def after(self, _delay, fn): fn()
    def withdraw(self): self.hidden = True
    def deiconify(self): self.hidden = False
    def lift(self): pass
    def attributes(self, *_args): pass
    def destroy(self): self.destroy_calls += 1


class _Service:
    def __init__(self): self.stop_calls = 0
    def stop(self, *args, **kwargs): self.stop_calls += 1


class _Warm:
    unload_on_exit = True
    def __init__(self): self.unload_calls = 0
    def unload(self, **kwargs): self.unload_calls += 1; return {"loaded": False}


class _Tray(_Service):
    def __init__(self, available=True): super().__init__(); self.available = available
    def status(self): return {"available": self.available, "degraded": not self.available}


class _UI:
    def __init__(self):
        self._closing = False
        self._accepting_commands = True
        self._recording = False
        self._hotkey_listener = _Service()
        self._ptt_listener = _Service()
        self.gaming_awareness = _Service()
        self.perception = _Service()
        self.llm_warm_manager = _Warm()
        self.input_entry = None
        self.agent = type("Agent", (), {"memory": None, "continuity": None})()


class ResidentLifecycleTests(unittest.TestCase):
    def make_manager(self, tray_available=True):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root, ui, tray = _Root(), _UI(), _Tray(tray_available)
        manager = RuntimeLifecycleManager(
            root,
            {"resident_mode": {"enabled": True, "close_to_tray": True}},
            ui=ui,
            scheduler=lambda fn: fn(),
            data_root=Path(temp.name),
        )
        manager.attach_tray(tray)
        return manager, root, ui, tray

    def test_running_hidden_running_does_not_stop_services(self):
        manager, root, ui, _tray = self.make_manager(True)
        manager.mark_running(); manager.request_window_close()
        self.assertEqual(manager.state, "hidden")
        self.assertTrue(root.hidden)
        self.assertFalse(ui._closing)
        self.assertEqual(ui.gaming_awareness.stop_calls, 0)
        self.assertEqual(ui.perception.stop_calls, 0)
        self.assertEqual(ui.llm_warm_manager.unload_calls, 0)
        self.assertEqual(ui._hotkey_listener.stop_calls, 0)
        manager.show_window()
        self.assertEqual(manager.state, "running")
        self.assertFalse(root.hidden)

    def test_tray_unavailable_makes_x_close_normally(self):
        manager, root, ui, _tray = self.make_manager(False)
        manager.mark_running(); manager.request_window_close()
        self.assertEqual(manager.state, "stopped")
        self.assertTrue(ui._closing)
        self.assertEqual(root.destroy_calls, 1)
        self.assertEqual(ui.llm_warm_manager.unload_calls, 1)

    def test_real_exit_is_idempotent(self):
        manager, root, ui, tray = self.make_manager(True)
        manager.mark_running()
        self.assertTrue(manager.request_shutdown("test"))
        self.assertFalse(manager.request_shutdown("test"))
        self.assertEqual(root.destroy_calls, 1)
        self.assertEqual(ui.gaming_awareness.stop_calls, 1)
        self.assertEqual(ui.perception.stop_calls, 1)
        self.assertEqual(ui.llm_warm_manager.unload_calls, 1)
        self.assertEqual(tray.stop_calls, 1)


class _Locker:
    def __init__(self): self.locked = False
    def acquire(self, _stream):
        if self.locked: return False
        self.locked = True; return True
    def release(self, _stream): self.locked = False


class InstanceRuntimeTests(unittest.TestCase):
    def test_second_instance_can_signal_show_and_lock_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td); locker = _Locker()
            first = InstanceLock(path=folder / "nova.lock", locker=locker)
            second = InstanceLock(path=folder / "nova.lock", locker=locker)
            mailbox = InstanceCommandMailbox(folder / "nova.command")
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertTrue(mailbox.send("show"))
            self.assertEqual(mailbox.consume()["command"], "show")
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_command_mailbox_rejects_malformed_input(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nova.command"
            path.write_text(json.dumps({"command": "anything"}), encoding="utf-8")
            message = InstanceCommandMailbox(path).consume()
            self.assertFalse(message["ok"])
            self.assertEqual(message["error"], "invalid_message")


class _Registry:
    def __init__(self): self.values = {}
    def read(self, name): return self.values.get(name)
    def write(self, name, value): self.values[name] = value
    def delete(self, name): self.values.pop(name, None)


class AutostartTests(unittest.TestCase):
    def test_enable_disable_is_idempotent_and_quotes_spaced_install(self):
        with tempfile.TemporaryDirectory(prefix="Nova Resident Test ") as td:
            root = Path(td)
            pyw = root / ".venv" / "Scripts" / "pythonw.exe"
            pyw.parent.mkdir(parents=True); pyw.write_text("", encoding="utf-8")
            (root / "app.py").write_text("", encoding="utf-8")
            backend = _Registry(); manager = AutostartManager(root, backend=backend)
            self.assertIn("--background", manager.command())
            self.assertIn('"', manager.command())
            self.assertTrue(manager.set_enabled(True))
            self.assertTrue(manager.is_enabled())
            self.assertTrue(manager.set_enabled(True))
            self.assertTrue(manager.set_enabled(False))
            self.assertFalse(manager.status()["present"])


class _Icon:
    def __init__(self): self.run_calls = 0; self.stop_calls = 0
    def run_detached(self): self.run_calls += 1
    def stop(self): self.stop_calls += 1
    def notify(self, *_args): pass
    def update_menu(self): pass


class TrayMockTests(unittest.TestCase):
    def test_tray_start_is_idempotent(self):
        icon = _Icon(); made = []
        ui = type("UI", (), {"config": {}, "agent": None})()
        lifecycle = type("Lifecycle", (), {"show_window": lambda self: True, "request_shutdown": lambda self, reason: True})()
        tray = TrayController(ui, lifecycle, icon_factory=lambda _tray: made.append(True) or icon)
        self.assertTrue(tray.start()); self.assertTrue(tray.start())
        self.assertEqual(len(made), 1); self.assertEqual(icon.run_calls, 1)
        tray.stop(); self.assertEqual(icon.stop_calls, 1)

    def test_tray_failure_is_degraded(self):
        ui = type("UI", (), {"config": {}, "agent": None})()
        lifecycle = object()
        tray = TrayController(ui, lifecycle, icon_factory=lambda _tray: (_ for _ in ()).throw(RuntimeError("tray unavailable")))
        self.assertFalse(tray.start())
        self.assertTrue(tray.degraded)
        self.assertFalse(tray.available)


if __name__ == "__main__":
    unittest.main()
