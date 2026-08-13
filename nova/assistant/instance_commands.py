from __future__ import annotations

"""Small, user-local command mailbox used by the secondary Nova launcher."""

import json
import os
from pathlib import Path
import tempfile

ALLOWED_COMMANDS = {"show", "shutdown_for_update", "status"}


class InstanceCommandMailbox:
    def __init__(self, path: Path):
        self.path = Path(path)

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
                json.dump({"command": command}, stream)
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
            if not isinstance(data, dict) or set(data) != {"command"}:
                return {"ok": False, "error": "invalid_message"}
            return {"ok": True, "command": self.validate(data.get("command"))}
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return {"ok": False, "error": "invalid_message"}

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
