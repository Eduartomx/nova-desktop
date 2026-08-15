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
        self.last_result: dict[str, Any] = {"ok": True, "action": "none"}

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
            present = actual is not None
            matches = bool(actual == desired)
            conflict = bool(present and not matches)
            return {
                "enabled": matches,
                "present": present,
                "matches_installation": matches,
                "conflict": conflict,
                "conflict_reason": "entry_belongs_to_other_installation" if conflict else "",
                "last_result": dict(self.last_result),
            }
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            return {
                "enabled": False,
                "present": False,
                "matches_installation": False,
                "conflict": False,
                "error": self.last_error,
                "last_result": dict(self.last_result),
            }

    def is_enabled(self) -> bool:
        return bool(self.status().get("enabled"))

    def configure(self, enabled: bool) -> dict[str, Any]:
        try:
            desired = self.command()
            actual = self._read()
            if enabled:
                if actual == desired:
                    result = {"ok": True, "enabled": True, "action": "unchanged"}
                elif actual is not None and actual != desired:
                    result = {
                        "ok": False,
                        "enabled": False,
                        "action": "conflict",
                        "conflict": True,
                        "error": "entry_belongs_to_other_installation",
                    }
                else:
                    self._write(desired)
                    result = {"ok": True, "enabled": True, "action": "created"}
            else:
                if actual is None:
                    result = {"ok": True, "enabled": False, "action": "unchanged"}
                elif actual != desired:
                    result = {
                        "ok": False,
                        "enabled": False,
                        "action": "conflict",
                        "conflict": True,
                        "error": "entry_belongs_to_other_installation",
                    }
                else:
                    self._delete()
                    result = {"ok": True, "enabled": False, "action": "removed"}
            self.last_error = ""
            self.last_result = result
            return dict(result)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            result = {"ok": False, "enabled": False, "action": "error", "error": self.last_error}
            self.last_result = result
            return dict(result)

    def set_enabled(self, enabled: bool) -> bool:
        """Compatibility wrapper; detailed callers should use ``configure``."""
        return bool(self.configure(bool(enabled)).get("ok"))
