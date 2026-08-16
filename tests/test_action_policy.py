from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from assistant.action_broker import EFFECT_CLASSES, policy_for, policy_registry


class ActionPolicyTests(unittest.TestCase):
    def test_every_tool_schema_has_a_registered_policy(self):
        code = (
            "from assistant.core_runtime import install_core_runtime; install_core_runtime(); "
            "from assistant.tools import TOOL_SCHEMAS; "
            "from assistant.action_broker import policy_registry,EFFECT_CLASSES; "
            "names={x['function']['name'] for x in TOOL_SCHEMAS}; r=policy_registry(names); "
            "assert set(r)==names; assert all(x.effect in EFFECT_CLASSES for x in r.values()); print(len(names))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "nova")
        run = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_unknown_and_forbidden_powershell_fail_closed(self):
        names = {"powershell"}
        self.assertEqual(policy_for("unknown", {}, known_tools=names).effect, "forbidden")
        for command in (
            "Remove-Item C:\\data -Recurse -Force", "Format-Volume C", "bcdedit /deletevalue safeboot",
            "Set-MpPreference -DisableRealtimeMonitoring $true", "reg delete HKLM\\SAM /f",
        ):
            with self.subTest(command=command):
                self.assertEqual(policy_for("powershell", {"command": command}, known_tools=names).effect, "forbidden")

    def test_submit_and_alternative_enter_are_high_risk(self):
        names = {"browser_fill", "browser_press", "keyboard_press", "uia_click", "mouse_click"}
        self.assertEqual(policy_for("browser_fill", {"submit": True}, known_tools=names).effect, "high_risk")
        for name in names - {"browser_fill"}:
            self.assertEqual(policy_for(name, {"key": "Enter"}, known_tools=names).effect, "high_risk")

    def test_legacy_confirmation_migration_never_weakens_enabled_flags(self):
        from assistant import config as config_mod
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({"security": {"confirm_file_writes": True, "confirm_powershell": False}}), encoding="utf-8")
            with mock.patch.object(config_mod, "CONFIG_PATH", path):
                migrated = config_mod.load_config()
            self.assertEqual(migrated["security"]["profile"], "safe")
            self.assertTrue(migrated["security"]["confirm_file_writes"])

            path.write_text(json.dumps({"security": {"confirm_file_writes": False, "confirm_powershell": False}}), encoding="utf-8")
            with mock.patch.object(config_mod, "CONFIG_PATH", path):
                migrated = config_mod.load_config()
            self.assertEqual(migrated["security"]["profile"], "trusted")


if __name__ == "__main__":
    unittest.main()
