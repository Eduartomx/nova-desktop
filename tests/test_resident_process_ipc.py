from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from assistant.instance_commands import InstanceCommandMailbox
from assistant.instance_lock import InstanceLock
from updater.update_runner import coordinate_runtime_shutdown


REPO_ROOT = Path(__file__).resolve().parents[1]
NOVA_ROOT = REPO_ROOT / "nova"


def _env():
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(NOVA_ROOT) + (os.pathsep + existing if existing else "")
    return env


def _wait_for_path(path: Path, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    event = threading.Event()
    while time.monotonic() < deadline:
        if path.exists():
            return True
        event.wait(0.02)
    return path.exists()


OWNER_SCRIPT = r'''
import json, sys, threading, time
from pathlib import Path
from assistant.instance_lock import InstanceLock
from assistant.instance_commands import InstanceCommandMailbox
lock = InstanceLock(path=Path(sys.argv[1]), owner_path=Path(sys.argv[2]))
if not lock.acquire():
    raise SystemExit(20)
mailbox = InstanceCommandMailbox(Path(sys.argv[3]), owner_id=lock.owner_id)
marker = Path(sys.argv[4])
print(json.dumps({"pid": __import__("os").getpid(), "owner_id": lock.owner_id}), flush=True)
deadline = time.monotonic() + 10.0
while time.monotonic() < deadline:
    message = mailbox.consume(owner_id=lock.owner_id)
    if message and message.get("ok"):
        if message.get("command") == "show":
            marker.write_text("show", encoding="utf-8")
        elif message.get("command") == "shutdown_for_update":
            threading.Event().wait(0.25)
            lock.release()
            raise SystemExit(0)
    threading.Event().wait(0.02)
lock.release()
raise SystemExit(21)
'''


SECONDARY_SCRIPT = r'''
import sys
from pathlib import Path
from assistant.instance_lock import InstanceLock
from assistant.instance_commands import InstanceCommandMailbox
lock = InstanceLock(path=Path(sys.argv[1]), owner_path=Path(sys.argv[2]))
if lock.acquire():
    lock.release()
    raise SystemExit(30)
owner = lock.read_owner()
if not owner:
    raise SystemExit(31)
mailbox = InstanceCommandMailbox(Path(sys.argv[3]))
raise SystemExit(0 if mailbox.send("show", target_owner_id=owner["owner_id"]) else 32)
'''


SENDER_SCRIPT = r'''
import sys
from pathlib import Path
from assistant.instance_commands import InstanceCommandMailbox
mailbox = InstanceCommandMailbox(Path(sys.argv[1]))
raise SystemExit(0 if mailbox.send(sys.argv[3], target_owner_id=sys.argv[2]) else 40)
'''


BLOCKING_OWNER_SCRIPT = r'''
import json, os, sys, threading
from pathlib import Path
from assistant.instance_lock import InstanceLock
lock = InstanceLock(path=Path(sys.argv[1]), owner_path=Path(sys.argv[2]))
if not lock.acquire():
    raise SystemExit(50)
print(json.dumps({"pid": os.getpid(), "owner_id": lock.owner_id}), flush=True)
threading.Event().wait(10.0)
lock.release()
'''


class ResidentProcessIPCTests(unittest.TestCase):
    def _paths(self, folder: Path):
        return folder / "runtime.lock", folder / "owner.json", folder / "commands"

    def test_two_process_launches_keep_one_runtime_show_first_and_updater_waits_for_real_exit(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            lock_path, owner_path, commands = self._paths(folder)
            marker = folder / "shown.marker"
            owner_proc = subprocess.Popen(
                [sys.executable, "-c", OWNER_SCRIPT, str(lock_path), str(owner_path), str(commands), str(marker)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_env(),
            )
            try:
                line = owner_proc.stdout.readline().strip()
                self.assertTrue(line, owner_proc.stderr.read())
                owner = json.loads(line)
                secondary = subprocess.run(
                    [sys.executable, "-c", SECONDARY_SCRIPT, str(lock_path), str(owner_path), str(commands)],
                    capture_output=True,
                    text=True,
                    env=_env(),
                    timeout=5,
                )
                self.assertEqual(secondary.returncode, 0, secondary.stderr)
                self.assertTrue(_wait_for_path(marker), "secondary launch did not restore first runtime")
                probe = InstanceLock(path=lock_path, owner_path=owner_path)
                self.assertFalse(probe.acquire(), "two runtimes acquired the same scoped lock")

                started = time.monotonic()
                result = coordinate_runtime_shutdown(
                    NOVA_ROOT,
                    timeout=5.0,
                    expected_pid=int(owner["pid"]),
                    lock_factory=lambda: InstanceLock(path=lock_path, owner_path=owner_path),
                    mailbox=InstanceCommandMailbox(commands),
                )
                elapsed = time.monotonic() - started
                self.assertTrue(result.ok, result.error)
                self.assertTrue(result.command_sent)
                self.assertTrue(result.process_terminated)
                self.assertTrue(result.lock_acquired)
                self.assertGreaterEqual(elapsed, 0.18, "updater returned before the owner completed its delayed exit")
                self.assertIsNotNone(owner_proc.poll(), "owner process is still alive after updater authorization")
            finally:
                if owner_proc.poll() is None:
                    owner_proc.terminate()
                    owner_proc.wait(timeout=3)

    def test_two_separate_senders_do_not_overwrite_commands(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            lock_path, owner_path, commands = self._paths(folder)
            owner = InstanceLock(path=lock_path, owner_path=owner_path)
            self.assertTrue(owner.acquire())
            try:
                processes = [
                    subprocess.Popen([sys.executable, "-c", SENDER_SCRIPT, str(commands), owner.owner_id, command], env=_env())
                    for command in ("show", "status")
                ]
                for proc in processes:
                    self.assertEqual(proc.wait(timeout=5), 0)
                mailbox = InstanceCommandMailbox(commands, owner_id=owner.owner_id)
                rows = [mailbox.consume(owner_id=owner.owner_id), mailbox.consume(owner_id=owner.owner_id)]
                self.assertTrue(all(row and row.get("ok") for row in rows))
                self.assertEqual({row["command"] for row in rows}, {"show", "status"})
                self.assertEqual(len({row["command_id"] for row in rows}), 2)
            finally:
                owner.release()

    def test_crash_with_pending_shutdown_is_ignored_by_immediate_new_generation(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            lock_path, owner_path, commands = self._paths(folder)
            old_proc = subprocess.Popen(
                [sys.executable, "-c", BLOCKING_OWNER_SCRIPT, str(lock_path), str(owner_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_env(),
            )
            try:
                line = old_proc.stdout.readline().strip()
                self.assertTrue(line, old_proc.stderr.read())
                old = json.loads(line)
                mailbox = InstanceCommandMailbox(commands)
                self.assertTrue(mailbox.send("shutdown_for_update", target_owner_id=old["owner_id"]))
                old_proc.terminate()
                old_proc.wait(timeout=5)

                new_owner = InstanceLock(path=lock_path, owner_path=owner_path)
                self.assertTrue(new_owner.acquire(), "kernel lock did not recover after unexpected termination")
                try:
                    self.assertNotEqual(new_owner.owner_id, old["owner_id"])
                    new_mailbox = InstanceCommandMailbox(commands, owner_id=new_owner.owner_id)
                    rejected = new_mailbox.consume(owner_id=new_owner.owner_id)
                    self.assertEqual(rejected, {"ok": False, "error": "wrong_owner"})
                    self.assertIsNone(new_mailbox.consume(owner_id=new_owner.owner_id))
                    self.assertTrue(new_owner.acquired)
                finally:
                    new_owner.release()
            finally:
                if old_proc.poll() is None:
                    old_proc.terminate()
                    old_proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
