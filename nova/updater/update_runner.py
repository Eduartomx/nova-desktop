from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def nova_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_version(root: Path) -> str:
    path = root / "NOVA_VERSION.txt"
    if not path.exists():
        return "0.0.0"
    return path.read_text(encoding="utf-8", errors="ignore").strip().lstrip("vV") or "0.0.0"


def console_python(root: Path) -> Path:
    candidate = root / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return candidate
    current = Path(sys.executable)
    if current.name.casefold() == "pythonw.exe":
        sibling = current.with_name("python.exe")
        if sibling.exists():
            return sibling
    return current


def wait_for_parent(pid: int | None, timeout: float = 20.0) -> None:
    if not pid or pid <= 0:
        return
    if os.name == "nt":
        try:
            import ctypes

            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not handle:
                return
            try:
                ctypes.windll.kernel32.WaitForSingleObject(handle, int(max(0.0, timeout) * 1000))
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            return
        except Exception:
            pass
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.15)


def status_path(root: Path) -> Path:
    return root / "data" / "update_last.json"


def write_status(root: Path, *, ok: bool, before: str, after: str, log: Path, error: str = "") -> None:
    path = status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": bool(ok),
        "before": before,
        "after": after,
        "error": str(error or ""),
        "log": str(log),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def launch_nova(root: Path) -> tuple[bool, str]:
    starter = root / "INICIAR.bat"
    try:
        if os.name == "nt" and starter.exists():
            os.startfile(str(starter))
            return True, str(starter)

        pyw = root / ".venv" / "Scripts" / "pythonw.exe"
        py = pyw if pyw.exists() else console_python(root)
        app = root / "app.py"
        if not app.exists():
            return False, f"No existe {app}"
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [str(py), str(app)],
            cwd=str(root),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True, f"{py} {app}"
    except Exception as exc:
        return False, str(exc)


def run_update(root: Path, log: Path) -> tuple[int, str]:
    py = console_python(root)
    updater = root / "updater" / "nova_updater.py"
    if not updater.exists():
        return 2, f"No existe {updater}"
    cmd = [str(py), str(updater), "--yes"]
    try:
        with open(log, "w", encoding="utf-8", errors="replace") as stream:
            stream.write("Nova Update Runner\n")
            stream.write("Comando: " + subprocess.list2cmdline(cmd) + "\n\n")
            stream.flush()
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
            )
        return int(proc.returncode), ""
    except Exception as exc:
        return 2, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervisa una actualización de Nova y relanza la aplicación.")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=20.0)
    args = parser.parse_args()

    root = nova_root()
    logs = root / "data" / "updater_logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / ("update_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
    before = read_version(root)

    wait_for_parent(args.parent_pid, args.wait_seconds)
    time.sleep(0.25)

    rc, runner_error = run_update(root, log)
    after = read_version(root)
    ok = rc == 0
    error = runner_error
    if not ok and not error:
        error = f"El updater terminó con código {rc}. Revisa {log}."

    write_status(root, ok=ok, before=before, after=after, log=log, error=error)

    launched, launch_detail = launch_nova(root)
    if not launched:
        with open(log, "a", encoding="utf-8", errors="replace") as stream:
            stream.write("\n[ERROR REINICIO] " + launch_detail + "\n")
        return 3 if ok else rc or 2
    return 0 if ok else rc or 2


if __name__ == "__main__":
    raise SystemExit(main())
