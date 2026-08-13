from __future__ import annotations

"""Small, user-local command mailbox used by the secondary Nova launcher."""

import json
import os
from pathlib import Path
import tempfile
import time

ALLOWED_COMMANDS = {"show", "shutdown_for_update", "status"}


class InstanceCommandMailbox:
    def __init__(self, path: Path, *, max_age_seconds: float = 8.0):
        self.path = Path(path)
        self.max_age_seconds = max(1.0, float(max_age_seconds))

    @staticmethod
    def validate(command: str) -> str:
        value = str(command or "").strip().casefold()
        if value not in ALLOWED_COMMANDS:
            raise ValueError("unsupported control command")
        return value

    def send(self, command: str) -> bool:
        command = self.validate(command)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(prefix="nova-runtime-command-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"command": command, "created_at": time.time()}, stream)
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    def consume(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
            self.path.unlink(missing_ok=True)
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"command", "created_at"}:
                return {"ok": False, "error": "invalid_message"}
            created = float(data.get("created_at") or 0.0)
            if created <= 0 or time.time() - created > self.max_age_seconds:
                return {"ok": False, "error": "stale_message"}
            return {"ok": True, "command": self.validate(data.get("command"))}
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": False, "error": "invalid_message"}

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
