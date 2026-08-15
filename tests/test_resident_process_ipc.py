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
from assistant.update_supervisor import create_supervisor_mutex
from updater.update_runner import SUPERVISOR_ALREADY_RUNNING_CODE, coordinate_runtime_shutdown

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


def _wait_for_owner(path: Path, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    event = threading.Event()
    while time.monotonic() < deadline:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("pid") and data.get("owner_id"):
                return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        event.wait(0.02)
    return {}


def _wait_for_mutex_available(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    event = threading.Event()
    while time.monotonic() < deadline:
        lock = create_supervisor_mutex(path=path)
        if lock.acquire():
            lock.release()
            return True
        event.wait(0.02)
    return False


OWNER_SCRIPT = r'''
import sys, threading, time
from pathlib import Path
from assistant.instance_lock import InstanceLock
from assistant.instance_commands import InstanceCommandMailbox
lock = InstanceLock(path=Path(sys.argv[1]), owner_path=Path(sys.argv[2]), role="runtime")
if not lock.acquire(): raise SystemExit(20)
mailbox = InstanceCommandMailbox(Path(sys.argv[3]), owner_id=lock.owner_id)
marker = Path(sys.argv[4])
deadline = time.monotonic() + 12.0
while time.monotonic() < deadline:
    message = mailbox.consume(owner_id=lock.owner_id)
    if message and message.get("ok"):
        if message.get("command") == "show": marker.write_text("show", encoding="utf-8")
        elif message.get("command") == "shutdown_for_update":
            threading.Event().wait(0.25)
            lock.release()
            raise SystemExit(0)
    threading.Event().wait(0.02)
lock.release(); raise SystemExit(21)
'''

SECONDARY_SCRIPT = r'''
import sys
from pathlib import Path
from assistant.instance_lock import InstanceLock
from assistant.instance_commands import InstanceCommandMailbox
lock = InstanceLock(path=Path(sys.argv[1]), owner_path=Path(sys.argv[2]), role="runtime")
if lock.acquire(): lock.release(); raise SystemExit(30)
owner = lock.read_owner()
if not owner or owner.get("role", "runtime") != "runtime": raise SystemExit(31)
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
import os, sys, threading, time
from pathlib import Path
from assistant.instance_lock import InstanceLock
lock = InstanceLock(path=Path(sys.argv[1]), owner_path=Path(sys.argv[2]), role="runtime")
die = Path(sys.argv[3])
if not lock.acquire(): raise SystemExit(50)
deadline = time.monotonic() + 30.0
event = threading.Event()
while time.monotonic() < deadline:
    if die.exists():
        os._exit(51)
    event.wait(0.02)
lock.release(); raise SystemExit(52)
'''

SUPERVISOR_SCRIPT = r'''
import os, sys, threading, time
from pathlib import Path
from assistant.update_supervisor import create_supervisor_mutex
from updater import update_runner
from updater.update_runner import ShutdownCoordination
root = Path(sys.argv[1])
mutex_path = Path(sys.argv[2])
markers = Path(sys.argv[3])
finish = Path(sys.argv[4])
token = str(sys.argv[5])
markers.mkdir(parents=True, exist_ok=True)
pid = os.getpid()
def mark(kind):
    (markers / (kind + "_" + token)).write_text(str(pid), encoding="utf-8")
class SupervisorMutex:
    def __init__(self):
        self._lock = create_supervisor_mutex(path=mutex_path)
    def acquire(self):
        ok = self._lock.acquire()
        if ok: mark("mutex")
        return ok
    def release(self):
        return self._lock.release()
class Guard:
    def release(self): mark("guard_release")
def coordinate(*_a, **_kw):
    mark("shutdown")
    return ShutdownCoordination(True, process_terminated=True, lock_acquired=True, guard=Guard())
def run_update(*_a, **_kw):
    mark("run")
    deadline = time.monotonic() + 30.0
    event = threading.Event()
    while time.monotonic() < deadline and not finish.exists(): event.wait(0.02)
    if not finish.exists(): return 9, "test owner timeout"
    return 0, ""
def launch(*_a, **_kw): mark("launch"); return True, "ok"
update_runner.nova_root = lambda: root
update_runner.coordinate_runtime_shutdown = coordinate
update_runner.run_update = run_update
update_runner.read_version = lambda *_a, **_kw: "0.9.8"
update_runner.write_status = lambda *_a, **_kw: None
update_runner.launch_nova = launch
rc = update_runner.main([], supervisor_lock_factory=SupervisorMutex)
mark("rc" + str(rc))
raise SystemExit(rc)
'''

SUPERVISOR_HOLDER_SCRIPT = r'''
import os, sys, threading, time
from pathlib import Path
from assistant.update_supervisor import create_supervisor_mutex
lock = create_supervisor_mutex(path=Path(sys.argv[1]))
held = Path(sys.argv[2])
die = Path(sys.argv[3])
if not lock.acquire(): raise SystemExit(70)
held.write_text(str(os.getpid()), encoding="utf-8")
deadline = time.monotonic() + 30.0
event = threading.Event()
while time.monotonic() < deadline:
    if die.exists():
        os._exit(71)
    event.wait(0.02)
lock.release(); raise SystemExit(72)
'''


@unittest.skipUnless(os.name == "nt", "real kernel resident integration is executed on windows-latest")
class ResidentProcessIPCTests(unittest.TestCase):
    def _paths(self, folder: Path):
        return folder / "runtime.lock", folder / "owner.json", folder / "commands"

    def test_two_process_launches_keep_one_runtime_show_first_and_updater_waits_for_real_exit(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            lock_path, owner_path, commands = self._paths(folder)
            marker = folder / "shown.marker"
            owner_proc = subprocess.Popen([sys.executable, "-c", OWNER_SCRIPT, str(lock_path), str(owner_path), str(commands), str(marker)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())
            result = None
            try:
                owner = _wait_for_owner(owner_path, 4.0)
                self.assertTrue(owner, f"runtime owner metadata was not published; rc={owner_proc.poll()}")
                self.assertGreater(int(owner.get("process_creation_time") or 0), 0)
                secondary = subprocess.run([sys.executable, "-c", SECONDARY_SCRIPT, str(lock_path), str(owner_path), str(commands)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env(), timeout=5)
                self.assertEqual(secondary.returncode, 0)
                self.assertTrue(_wait_for_path(marker))
                probe = InstanceLock(path=lock_path, owner_path=owner_path, publish_owner=False)
                self.assertFalse(probe.acquire())
                started = time.monotonic()
                result = coordinate_runtime_shutdown(NOVA_ROOT, timeout=5.0, expected_pid=int(owner["pid"]), lock_factory=lambda: InstanceLock(path=lock_path, owner_path=owner_path, publish_owner=False, role="observer"), guard_factory=lambda: InstanceLock(path=lock_path, owner_path=owner_path, publish_owner=False, role="updater"), mailbox=InstanceCommandMailbox(commands))
                elapsed = time.monotonic() - started
                self.assertTrue(result.ok, result.error)
                self.assertTrue(result.command_sent)
                self.assertTrue(result.process_terminated)
                self.assertTrue(result.lock_acquired)
                self.assertGreaterEqual(elapsed, 0.18)
                self.assertIsNotNone(owner_proc.poll())
                competing = InstanceLock(path=lock_path, owner_path=owner_path, publish_owner=False)
                self.assertFalse(competing.acquire())
                metadata = InstanceLock(path=lock_path, owner_path=owner_path, publish_owner=False).read_owner()
                self.assertEqual(metadata.get("role"), "updater")
            finally:
                if result is not None: result.release_guard()
                if owner_proc.poll() is None:
                    owner_proc.terminate(); owner_proc.wait(timeout=3)

    def test_two_separate_senders_do_not_overwrite_commands(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td); lock_path, owner_path, commands = self._paths(folder)
            owner = InstanceLock(path=lock_path, owner_path=owner_path, role="runtime")
            self.assertTrue(owner.acquire())
            try:
                processes = [subprocess.Popen([sys.executable, "-c", SENDER_SCRIPT, str(commands), owner.owner_id, command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env()) for command in ("show", "status")]
                for proc in processes: self.assertEqual(proc.wait(timeout=5), 0)
                mailbox = InstanceCommandMailbox(commands, owner_id=owner.owner_id)
                rows = [mailbox.consume(owner_id=owner.owner_id), mailbox.consume(owner_id=owner.owner_id)]
                self.assertTrue(all(row and row.get("ok") for row in rows))
                self.assertEqual({row["command"] for row in rows}, {"show", "status"})
                self.assertEqual(len({row["command_id"] for row in rows}), 2)
            finally: owner.release()

    def test_crash_with_pending_shutdown_is_ignored_by_immediate_new_generation(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td); lock_path, owner_path, commands = self._paths(folder); die = folder / "die.marker"
            old_proc = subprocess.Popen([sys.executable, "-c", BLOCKING_OWNER_SCRIPT, str(lock_path), str(owner_path), str(die)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())
            try:
                old = _wait_for_owner(owner_path, 4.0); self.assertTrue(old)
                self.assertGreater(int(old.get("process_creation_time") or 0), 0)
                mailbox = InstanceCommandMailbox(commands)
                self.assertTrue(mailbox.send("shutdown_for_update", target_owner_id=old["owner_id"]))
                die.write_text("die", encoding="utf-8")
                old_proc.wait(timeout=5)
                self.assertTrue(_wait_for_mutex_available(lock_path, 5.0))
                new_owner = InstanceLock(path=lock_path, owner_path=owner_path, role="runtime")
                self.assertTrue(new_owner.acquire())
                try:
                    self.assertNotEqual(new_owner.owner_id, old["owner_id"])
                    new_mailbox = InstanceCommandMailbox(commands, owner_id=new_owner.owner_id)
                    self.assertEqual(new_mailbox.consume(owner_id=new_owner.owner_id), {"ok": False, "error": "wrong_owner"})
                    self.assertIsNone(new_mailbox.consume(owner_id=new_owner.owner_id))
                finally: new_owner.release()
            finally:
                die.write_text("die", encoding="utf-8")
                if old_proc.poll() is None: old_proc.terminate(); old_proc.wait(timeout=3)

    def test_two_real_supervisors_only_owner_coordinates_updates_and_launches(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td); root = folder / "nova"; root.mkdir()
            mutex = folder / "scope" / "update_supervisor.lock"; markers = folder / "markers"; finish = folder / "finish.marker"
            first = subprocess.Popen([sys.executable, "-c", SUPERVISOR_SCRIPT, str(root), str(mutex), str(markers), str(finish), "first"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=_env())
            second = None
            try:
                mutex_marker = markers / "mutex_first"
                acquired = _wait_for_path(mutex_marker, 20.0)
                first_error = first.stderr.read() if (not acquired and first.poll() is not None) else ""
                self.assertTrue(acquired, f"first supervisor never acquired mutex; launcher_rc={first.poll()} stderr={first_error}")
                second = subprocess.Popen([sys.executable, "-c", SUPERVISOR_SCRIPT, str(root), str(mutex), str(markers), str(finish), "second"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=_env())
                second_rc = second.wait(timeout=20)
                self.assertEqual(second_rc, SUPERVISOR_ALREADY_RUNNING_CODE, second.stderr.read())
                self.assertFalse((markers / "mutex_second").exists())
                self.assertFalse((markers / "shutdown_second").exists())
                self.assertFalse((markers / "run_second").exists())
                self.assertFalse((markers / "launch_second").exists())
                self.assertTrue((markers / f"rc{SUPERVISOR_ALREADY_RUNNING_CODE}_second").exists())
                run_marker = markers / "run_first"
                reached_run = _wait_for_path(run_marker, 20.0)
                first_error = first.stderr.read() if (not reached_run and first.poll() is not None) else ""
                self.assertTrue(reached_run, f"mutex owner never reached run_update; launcher_rc={first.poll()} stderr={first_error}")
                finish.write_text("finish", encoding="utf-8")
                first_rc = first.wait(timeout=20)
                self.assertEqual(first_rc, 0, first.stderr.read())
                self.assertTrue((markers / "guard_release_first").exists())
                self.assertTrue((markers / "launch_first").exists())
                self.assertEqual(len(list(markers.glob("mutex_*"))), 1)
                self.assertEqual(len(list(markers.glob("shutdown_*"))), 1)
                self.assertEqual(len(list(markers.glob("run_*"))), 1)
                self.assertEqual(len(list(markers.glob("launch_*"))), 1)
            finally:
                finish.write_text("finish", encoding="utf-8")
                for proc in (second, first):
                    if proc is not None and proc.poll() is None:
                        proc.terminate(); proc.wait(timeout=5)

    def test_dead_supervisor_process_does_not_leave_mutex_owned(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td); mutex = folder / "scope" / "update_supervisor.lock"; held = folder / "held.marker"; die = folder / "die.marker"
            proc = subprocess.Popen([sys.executable, "-c", SUPERVISOR_HOLDER_SCRIPT, str(mutex), str(held), str(die)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())
            try:
                self.assertTrue(_wait_for_path(held, 5.0))
                competing = create_supervisor_mutex(path=mutex)
                self.assertFalse(competing.acquire())
                die.write_text("die", encoding="utf-8")
                proc.wait(timeout=5)
                self.assertTrue(_wait_for_mutex_available(mutex, 5.0))
            finally:
                die.write_text("die", encoding="utf-8")
                if proc.poll() is None: proc.terminate(); proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
