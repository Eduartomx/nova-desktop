from __future__ import annotations

import builtins
import json
from pathlib import Path
import shutil
import tempfile
import types
import unittest
from unittest import mock

import app
from updater.recovery_state import prepare_stable_recovery_runtime


class StableRecoveryBootstrapTests(unittest.TestCase):
    def _root(self, td: str) -> Path:
        root = Path(td) / "nova"
        (root / "updater").mkdir(parents=True)
        (root / "data").mkdir()
        source = Path(__file__).resolve().parents[1] / "nova" / "updater"
        for name in (
            "recovery_journal.py", "recovery_attempts.py", "recovery_files.py",
            "recovery_environment.py", "recovery_state.py", "recovery_locking.py",
            "recovery_bootstrap.py",
        ):
            shutil.copy2(source / name, root / "updater" / name)
        (root / "app.py").write_text("# fixture\n", encoding="utf-8")
        return root

    def test_stable_generation_is_hash_validated_and_previous_copy_survives_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            first = prepare_stable_recovery_runtime(root)
            second = prepare_stable_recovery_runtime(root)
            runtime = root / "data" / "recovery_runtime"
            self.assertNotEqual(first["generation"], second["generation"])
            self.assertTrue((runtime / "generations" / first["generation"] / "recovery_bootstrap.py").is_file())
            active = json.loads((runtime / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["generation"], second["generation"])
            stable = app._load_stable_recovery_bootstrap(root)
            self.assertTrue(callable(stable.startup_recovery_gate))

    def test_tampered_stable_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            manifest = prepare_stable_recovery_runtime(root)
            target = root / "data" / "recovery_runtime" / "generations" / manifest["generation"]
            (target / "recovery_state.py").write_text("tampered", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                app._load_stable_recovery_bootstrap(root)

    def test_managed_import_failure_uses_stable_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            (root / "data" / "update_recovery.json").write_text('{"state":"transaction_prepared"}', encoding="utf-8")
            fake_result = types.SimpleNamespace(
                continue_startup=False, recovered=False, launched=False, exit_code=7
            )
            stable = types.SimpleNamespace(startup_recovery_gate=mock.Mock(return_value=fake_result))
            real_import = builtins.__import__
            def guarded_import(name, *args, **kwargs):
                if name == "updater.recovery_bootstrap":
                    raise ImportError("managed bootstrap broken")
                return real_import(name, *args, **kwargs)
            with mock.patch.object(app, "__file__", str(root / "app.py")), \
                 mock.patch("builtins.__import__", side_effect=guarded_import), \
                 mock.patch.object(app, "_load_stable_recovery_bootstrap", return_value=stable) as load_stable, \
                 mock.patch.object(app, "_native_recovery_notice") as notice:
                allowed, code = app._startup_recovery_gate([])
            self.assertFalse(allowed)
            self.assertEqual(code, 7)
            self.assertEqual(load_stable.call_count, 1)
            stable_root = Path(load_stable.call_args.args[0])
            self.assertEqual(stable_root.resolve(), root.resolve())
            self.assertEqual(stable.startup_recovery_gate.call_count, 1)
            startup_root = Path(stable.startup_recovery_gate.call_args.args[0])
            self.assertEqual(startup_root.resolve(), root.resolve())
            notice.assert_not_called()

    def test_both_bootstraps_broken_use_native_notice_and_code_seven(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            (root / "data" / "update_recovery.json").write_text('{"state":"files_applying"}', encoding="utf-8")
            real_import = builtins.__import__
            def guarded_import(name, *args, **kwargs):
                if name == "updater.recovery_bootstrap":
                    raise ImportError("managed broken")
                return real_import(name, *args, **kwargs)
            with mock.patch.object(app, "__file__", str(root / "app.py")), \
                 mock.patch("builtins.__import__", side_effect=guarded_import), \
                 mock.patch.object(app, "_load_stable_recovery_bootstrap", side_effect=RuntimeError("stable broken")), \
                 mock.patch.object(app, "_native_recovery_notice") as notice:
                allowed, code = app._startup_recovery_gate([])
            self.assertFalse(allowed)
            self.assertEqual(code, 7)
            notice.assert_called_once()
            self.assertEqual(notice.call_args.kwargs["state"], "files_applying")

    def test_pythonw_path_uses_native_message_before_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            class Function:
                def __init__(self): self.calls = []
                def __call__(self, *args): self.calls.append(args); return 1
            message_box = Function()
            fake_user32 = types.SimpleNamespace(MessageBoxW=message_box)
            with mock.patch.object(app.sys, "platform", "win32"), \
                 mock.patch.object(app.sys, "executable", str(root / "pythonw.exe")), \
                 mock.patch.object(app.ctypes, "WinDLL", return_value=fake_user32, create=True), \
                 mock.patch.object(app.sys, "stderr", None):
                app._native_recovery_notice(root, state="dependency_repair_required", detail="repair needed")
            self.assertEqual(len(message_box.calls), 1)
            self.assertIn("dependency_repair_required", message_box.calls[0][1])

    def test_recovery_gate_precedes_claim_instance(self):
        with mock.patch.object(app.sys, "platform", "win32"), \
             mock.patch.object(app, "_startup_recovery_gate", return_value=(False, 7)) as gate, \
             mock.patch.object(app, "_claim_instance") as claim:
            self.assertEqual(app.main([]), 7)
        gate.assert_called_once()
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
