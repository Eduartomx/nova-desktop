from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from updater import nova_updater


class ResidentUpdaterTests(unittest.TestCase):
    def test_direct_update_delegates_to_supervisor_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            updater_dir = root / "updater"
            updater_dir.mkdir()
            runner = updater_dir / "update_runner.py"
            runner.write_text("# test\n", encoding="utf-8")
            with mock.patch.object(nova_updater, "ROOT", root):
                with mock.patch("updater.nova_updater.subprocess.call", return_value=0) as call:
                    result = nova_updater._delegate_direct_update()
        self.assertEqual(result, 0)
        call.assert_called_once()
        command = call.call_args.args[0]
        self.assertTrue(str(command[-1]).endswith("update_runner.py"))

    def test_check_path_remains_non_supervised(self):
        with mock.patch.object(nova_updater, "check_only", return_value=10) as check:
            with mock.patch.object(nova_updater, "_delegate_direct_update") as delegated:
                with mock.patch("sys.argv", ["nova_updater.py", "--check"]):
                    result = nova_updater.main()
        self.assertEqual(result, 10)
        check.assert_called_once()
        delegated.assert_not_called()


if __name__ == "__main__":
    unittest.main()
