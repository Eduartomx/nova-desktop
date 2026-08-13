from __future__ import annotations

"""Per-user Windows autostart setting for Nova."""

import os
from pathlib import Path
import subprocess
import sys
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "NovaDesktop"


class AutostartManager:
    def __init__(self, nova_root: Path | None = None, *, backend=None):
        self.root = Path(nova_root) if nova_root is not None else Path(__file__).resolve().parent.parent
        self.backend = backend
        self.last_error = ""

    def command(self) -> str:
        pyw = self.root / ".venv" / "Scripts" / "pythonw.exe"
        python = pyw if pyw.exists() else Path(sys.executable)
        app = self.root / "app.py"
        return subprocess.list2cmdline([str(python), str(app), "--background"])

    def _read(self):
        if self.backend is not None:
            return self.backend.read(VALUE_NAME)
        if os.name != "nt":
            return None
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
                return str(value)
        except FileNotFoundError:
            return None

    def _write(self, value: str):
        if self.backend is not None:
            self.backend.write(VALUE_NAME, value)
            return
        if os.name != "nt":
            raise OSError("Windows autostart is unavailable")
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)

    def _delete(self):
        if self.backend is not None:
            self.backend.delete(VALUE_NAME)
            return
        if os.name != "nt":
            return
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass

    def status(self) -> dict[str, Any]:
        try:
            actual = self._read()
            desired = self.command()
            return {"enabled": bool(actual == desired), "present": actual is not None, "matches_installation": bool(actual == desired)}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            return {"enabled": False, "present": False, "matches_installation": False, "error": self.last_error}

    def is_enabled(self) -> bool:
        return bool(self.status().get("enabled"))

    def set_enabled(self, enabled: bool) -> bool:
        try:
            if enabled:
                self._write(self.command())
            else:
                self._delete()
            self.last_error = ""
            return self.is_enabled() if enabled else not bool(self.status().get("present"))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            return False
