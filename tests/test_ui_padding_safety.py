from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from assistant.runtime_lifecycle import RuntimeLifecycleManager


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
        manager.mark_running()
        manager.request_window_close()
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
        manager.mark_running()
        manager.request_window_close()
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


if __name__ == "__main__":
    unittest.main()
