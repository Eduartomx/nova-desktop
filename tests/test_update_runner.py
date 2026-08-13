from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from updater.update_runner import (
    console_python,
    launch_nova,
    read_version,
    request_runtime_shutdown,
    status_path,
    write_status,
)


class UpdateRunnerTests(unittest.TestCase):
    def test_version_and_status_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "NOVA_VERSION.txt").write_text("0.6.2\n", encoding="utf-8")
            self.assertEqual(read_version(root), "0.6.2")

            log = root / "data" / "updater_logs" / "test.log"
            log.parent.mkdir(parents=True)
            log.write_text("ok", encoding="utf-8")
            write_status(root, ok=True, before="0.6.1", after="0.6.2", log=log)

            path = status_path(root)
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["before"], "0.6.1")
            self.assertEqual(data["after"], "0.6.2")
            self.assertEqual(data["log"], str(log))

    def test_missing_version_has_safe_default(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_version(Path(td)), "0.0.0")

    def test_console_python_returns_a_path(self):
        with tempfile.TemporaryDirectory() as td:
            result = console_python(Path(td))
            self.assertIsInstance(result, Path)
            self.assertTrue(str(result))

    def test_runtime_update_requests_real_exit_before_files_change(self):
        class Lock:
            def __init__(self, acquired): self._acquired = acquired
            def acquire(self): return self._acquired
            def release(self): pass

        states = iter([False, True])
        mailbox_calls = []
        mailbox = type("Mailbox", (), {"send": lambda self, command: mailbox_calls.append(command) or True})()
        ok = request_runtime_shutdown(
            Path(tempfile.gettempdir()),
            timeout=0.5,
            lock_factory=lambda: Lock(next(states)),
            mailbox=mailbox,
        )
        self.assertTrue(ok)
        self.assertEqual(mailbox_calls, ["shutdown_for_update"])

    def test_post_update_relaunches_exactly_one_visible_instance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("# test\n", encoding="utf-8")
            with patch("updater.update_runner.subprocess.Popen") as popen:
                ok, _detail = launch_nova(root)
            self.assertTrue(ok)
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[-1], "--post-update")


if __name__ == "__main__":
    unittest.main()
