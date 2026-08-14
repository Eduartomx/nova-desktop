from __future__ import annotations

"""Stdlib-only launch handoff for validated updater/recovery attempts.

The supervisor spawns a hash-validated copy of ``recovery_bootstrap.py`` while
the journal is still quarantined.  Only after Popen succeeds does the owner CAS
the journal to ``cleared``.  The helper waits for that exact attempt to become
cleared before launching Nova, so a supervisor death before the CAS cannot
start the application on an active attempt.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

try:
    from .recovery_journal import RECOVERY_REQUIRED_EXIT_CODE, RecoveryJournalError
except ImportError:
    from recovery_journal import RECOVERY_REQUIRED_EXIT_CODE, RecoveryJournalError

VALIDATED_STATES = {"update_validated", "rollback_validation_completed"}
HANDOFF_MODES = {"post-update": "--post-update", "post-recovery": "--post-recovery"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_bootstrap_path(root: Path) -> Path:
    """Return the active bootstrap only after verifying pointer, manifest and files."""
    root = Path(root)
    runtime = root / "data" / "recovery_runtime"
    active = runtime / "active.json"
    if not active.is_file() or active.is_symlink():
        raise RecoveryJournalError("stable_recovery_active_missing")
    try:
        pointer = json.loads(active.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecoveryJournalError(f"stable_recovery_active_corrupt:{type(exc).__name__}") from exc
    if not isinstance(pointer, dict) or int(pointer.get("schema_version") or 0) != 1:
        raise RecoveryJournalError("stable_recovery_active_schema")
    generation = str(pointer.get("generation") or "")
    if len(generation) != 32 or any(ch not in "0123456789abcdef" for ch in generation.lower()):
        raise RecoveryJournalError("stable_recovery_generation_invalid")
    generations = runtime / "generations"
    target = generations / generation
    if generations.is_symlink() or target.is_symlink() or not target.is_dir():
        raise RecoveryJournalError("stable_recovery_generation_unavailable")
    base = generations.resolve(strict=True)
    resolved = target.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise RecoveryJournalError("stable_recovery_generation_escape") from exc
    manifest_path = resolved / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RecoveryJournalError("stable_recovery_manifest_missing")
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != str(pointer.get("manifest_sha256") or "").lower():
        raise RecoveryJournalError("stable_recovery_manifest_hash_mismatch")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except Exception as exc:
        raise RecoveryJournalError(f"stable_recovery_manifest_corrupt:{type(exc).__name__}") from exc
    if not isinstance(manifest, dict) or int(manifest.get("schema_version") or 0) not in (1, 2):
        raise RecoveryJournalError("stable_recovery_manifest_schema")
    if str(manifest.get("generation") or "") != generation:
        raise RecoveryJournalError("stable_recovery_manifest_generation")
    files = manifest.get("files")
    if not isinstance(files, dict) or "recovery_bootstrap.py" not in files:
        raise RecoveryJournalError("stable_recovery_manifest_files")
    for name, expected in files.items():
        if not isinstance(name, str) or Path(name).name != name:
            raise RecoveryJournalError("stable_recovery_filename_invalid")
        path = resolved / name
        if not path.is_file() or path.is_symlink() or _sha256(path) != str(expected or "").lower():
            raise RecoveryJournalError(f"stable_recovery_hash_mismatch:{name}")
    return resolved / "recovery_bootstrap.py"


def spawn_handoff_helper(
    root: Path,
    attempt_id: str,
    mode: str,
    *,
    launcher=None,
    timeout_seconds: float = 20.0,
) -> tuple[bool, str]:
    if mode not in HANDOFF_MODES:
        return False, "invalid_handoff_mode"
    try:
        bootstrap = stable_bootstrap_path(root)
        command = [
            sys.executable,
            str(bootstrap),
            "--handoff-launch",
            "--root",
            str(Path(root)),
            "--attempt-id",
            str(attempt_id),
            "--handoff-mode",
            mode,
            "--handoff-timeout",
            str(max(1.0, min(float(timeout_seconds), 60.0))),
        ]
        call = launcher or subprocess.Popen
        kwargs: dict[str, Any] = {"cwd": str(Path(root)), "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        call(command, **kwargs)
        return True, ""
    except Exception as exc:
        return False, f"handoff_spawn_failed:{type(exc).__name__}"


def wait_for_cleared_attempt(root: Path, attempt_id: str, *, timeout_seconds: float = 20.0) -> tuple[bool, str]:
    """Wait until the exact validated attempt is CAS-cleared by its supervisor."""
    try:
        from .recovery_state import load_journal
    except ImportError:
        from recovery_state import load_journal
    deadline = time.monotonic() + max(1.0, min(float(timeout_seconds), 60.0))
    while True:
        try:
            journal = load_journal(Path(root))
        except Exception as exc:
            return False, f"handoff_journal_invalid:{type(exc).__name__}"
        if journal is None:
            return False, "handoff_journal_missing"
        if str(journal.get("attempt_id") or "") != str(attempt_id):
            return False, "handoff_attempt_changed"
        state = str(journal.get("state") or "")
        if state == "cleared" and not bool(journal.get("recovery_required")):
            return True, "cleared"
        if state not in VALIDATED_STATES or not bool(journal.get("recovery_required")):
            return False, f"handoff_state_invalid:{state or 'unknown'}"
        if time.monotonic() >= deadline:
            return False, "handoff_clear_timeout"
        time.sleep(0.05)


def launch_nova_after_clear(
    root: Path,
    attempt_id: str,
    mode: str,
    *,
    timeout_seconds: float = 20.0,
    launcher=None,
) -> tuple[bool, str]:
    ok, detail = wait_for_cleared_attempt(root, attempt_id, timeout_seconds=timeout_seconds)
    if not ok:
        return False, detail
    flag = HANDOFF_MODES.get(mode)
    if flag is None:
        return False, "invalid_handoff_mode"
    try:
        command = [sys.executable, str(Path(root) / "app.py"), flag]
        call = launcher or subprocess.Popen
        kwargs: dict[str, Any] = {"cwd": str(Path(root)), "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        call(command, **kwargs)
        return True, ""
    except Exception as exc:
        return False, f"nova_launch_failed:{type(exc).__name__}"
