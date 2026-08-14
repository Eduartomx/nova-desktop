from __future__ import annotations

"""File-only IPC for Nova resident runtime.

Each command is a separate atomic file addressed to one concrete ``owner_id``.
There is no socket, listener or network surface, and stale commands from an old
runtime generation are deleted instead of being delivered to a new runtime.
"""

import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any

ALLOWED_COMMANDS = {"show", "shutdown_for_update", "status"}


class InstanceCommandMailbox:
    def __init__(self, directory: Path, *, owner_id: str = "", max_age_seconds: float = 15.0):
        raw = Path(directory)
        # Compatibility with the first v0.9.9 draft, which passed nova.command.
        if raw.suffix or raw.name.endswith(".command"):
            raw = raw.parent / "commands"
        self.directory = raw
        self.owner_id = str(owner_id or "")
        self.max_age_seconds = max(1.0, float(max_age_seconds))

    @staticmethod
    def validate(command: str) -> str:
        value = str(command or "").strip().casefold()
        if value not in ALLOWED_COMMANDS:
            raise ValueError("unsupported control command")
        return value

    @staticmethod
    def _validate_owner_id(owner_id: str) -> str:
        value = str(owner_id or "").strip().lower()
        if len(value) < 16 or len(value) > 128 or any(ch not in "0123456789abcdef-" for ch in value):
            raise ValueError("invalid target owner")
        return value

    def send(self, command: str, *, target_owner_id: str | None = None) -> bool:
        command = self.validate(command)
        target = self._validate_owner_id(target_owner_id or self.owner_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "target_owner_id": target,
            "command": command,
            "created_at": time.time(),
        }
        final = self.directory / f"{time.time_ns()}-{command_id}.json"
        tmp = None
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="nova-command-", suffix=".tmp", dir=str(self.directory))
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
            os.replace(tmp, final)
            return True
        except Exception:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def _read_message(self, path: Path) -> tuple[dict[str, Any] | None, str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None, "invalid_message"
            required = {"command_id", "target_owner_id", "command", "created_at"}
            if set(data) != required:
                return None, "invalid_message"
            command_id = str(data.get("command_id") or "")
            target = self._validate_owner_id(str(data.get("target_owner_id") or ""))
            command = self.validate(str(data.get("command") or ""))
            created = float(data.get("created_at") or 0.0)
            if len(command_id) < 16 or created <= 0:
                return None, "invalid_message"
            if abs(time.time() - created) > self.max_age_seconds:
                return None, "stale_message"
            return {
                "command_id": command_id,
                "target_owner_id": target,
                "command": command,
                "created_at": created,
            }, ""
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return None, "invalid_message"

    def consume(self, *, owner_id: str | None = None) -> dict[str, Any] | None:
        target_owner = self._validate_owner_id(owner_id or self.owner_id)
        try:
            paths = sorted(self.directory.glob("*.json"))
        except OSError:
            return None
        first_error = None
        for path in paths:
            data, error = self._read_message(path)
            if data is None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                first_error = first_error or error
                continue
            if data["target_owner_id"] != target_owner:
                # No other runtime can validly own this same scoped mailbox.
                # Therefore a foreign target necessarily belongs to an old generation.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                first_error = first_error or "wrong_owner"
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                # Another consumer already claimed it; continue without duplicate delivery.
                continue
            except OSError:
                return {"ok": False, "error": "claim_failed"}
            return {"ok": True, **data}
        if first_error:
            return {"ok": False, "error": first_error}
        return None

    def purge_foreign(self, *, owner_id: str | None = None) -> int:
        target_owner = self._validate_owner_id(owner_id or self.owner_id)
        removed = 0
        try:
            paths = list(self.directory.glob("*.json"))
        except OSError:
            return 0
        for path in paths:
            data, _error = self._read_message(path)
            if data is None or data.get("target_owner_id") != target_owner:
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
        return removed

    def clear(self) -> None:
        try:
            for path in self.directory.glob("*.json"):
                path.unlink(missing_ok=True)
        except OSError:
            pass
