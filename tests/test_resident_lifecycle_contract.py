from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.runtime_lifecycle import RuntimeLifecycleManager


class Root:
    def __init__(self):
        self.hidden = False
        self.destroy_calls = 0
    def after(self, _delay, callback): callback()
    def withdraw(self): self.hidden = True
    def deiconify(self): self.hidden = False
    def lift(self): pass
    def attributes(self, *_args): pass
    def destroy(self): self.destroy_calls += 1


class Service:
    def __init__(self): self.stop_calls = 0
    def stop(self, *args, **kwargs): self.stop_calls += 1


class WakeStop:
    def __init__(self): self.set_calls = 0
    def set(self): self.set_calls += 1


class Warm:
    unload_on_exit = True
    def __init__(self): self.unload_calls = 0
    def unload(self, **_kwargs): self.unload_calls += 1; return {"loaded": False}


class Tray(Service):
    available = True
    def status(self): return {"available": True, "degraded": False}


class Instance:
    def __init__(self): self.release_calls = 0; self.acquired = True
    def release(self): self.release_calls += 1; self.acquired = False
    def status(self): return {"acquired": self.acquired}


class UI:
    def __init__(self):
        self._closing = False
        self._accepting_commands = True
        self._recording = False
        self._wake_stop = WakeStop()
        self._wake_thread = None
        self._hotkey_listener = Service()
        self._ptt_listener = Service()
        self.gaming_awareness = Service()
        self.perception = Service()
        self.llm_warm_manager = Warm()
        self.input_entry = None
        self.agent = type("Agent", (), {"memory": None, "continuity": None})()


class ResidentLifecycleContractTests(unittest.TestCase):
    def make(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root, ui, tray, instance = Root(), UI(), Tray(), Instance()
        manager = RuntimeLifecycleManager(
            root,
            {"resident_mode": {"enabled": True, "close_to_tray": True}},
            ui=ui,
            scheduler=lambda callback: callback(),
            data_root=Path(temp.name),
        )
        manager.attach_tray(tray)
        manager.attach_instance(instance)
        manager.mark_running()
        return manager, root, ui, tray, instance

    def test_hidden_runtime_keeps_wake_hotkeys_gaming_perception_and_qwen(self):
        manager, root, ui, _tray, _instance = self.make()
        manager.request_window_close()
        self.assertEqual(manager.state, "hidden")
        self.assertTrue(root.hidden)
        self.assertEqual(ui._wake_stop.set_calls, 0)
        self.assertEqual(ui._hotkey_listener.stop_calls, 0)
        self.assertEqual(ui._ptt_listener.stop_calls, 0)
        self.assertEqual(ui.gaming_awareness.stop_calls, 0)
        self.assertEqual(ui.perception.stop_calls, 0)
        self.assertEqual(ui.llm_warm_manager.unload_calls, 0)

    def test_tray_exit_releases_instance_and_unloads_qwen_once(self):
        manager, root, ui, tray, instance = self.make()
        self.assertTrue(manager.request_shutdown("tray_exit"))
        self.assertFalse(manager.request_shutdown("tray_exit"))
        self.assertEqual(instance.release_calls, 1)
        self.assertFalse(instance.acquired)
        self.assertEqual(ui.llm_warm_manager.unload_calls, 1)
        self.assertEqual(tray.stop_calls, 1)
        self.assertEqual(root.destroy_calls, 1)


if __name__ == "__main__":
    unittest.main()
