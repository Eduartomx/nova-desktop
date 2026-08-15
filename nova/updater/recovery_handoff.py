from __future__ import annotations

"""Stdlib-only launch handoff for validated updater/recovery attempts.

A validated transaction remains quarantined until a stable helper exists and the
runtime/recovery guard has been released. The helper is spawned first, but it
cannot launch Nova until the exact attempt is CAS-cleared by its supervisor.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import time
from typing import Any, Callable

try:
    from .recovery_journal import (
        RECOVERY_REQUIRED_EXIT_CODE,
        RecoveryJournalError,
        StaleJournalWriterError,
    )
except ImportError:
    from recovery_journal import (
        RECOVERY_REQUIRED_EXIT_CODE,
        RecoveryJournalError,
        StaleJournalWriterError,
    )

try:
    from .process_launch import (
        detached_hidden_creation_flags,
        select_console_python,
        select_gui_python,
    )
except ImportError:
    from process_launch import (
        detached_hidden_creation_flags,
        select_console_python,
        select_gui_python,
    )

VALIDATED_STATES = {"update_validated", "rollback_validation_completed"}
HANDOFF_MODES = {"post-update": "--post-update", "post-recovery": "--post-recovery"}
STABLE_RUNTIME_FILES = {
    "process_launch.py",
    "recovery_journal.py",
    "recovery_attempts.py",
    "recovery_files.py",
    "recovery_environment.py",
    "recovery_state.py",
    "recovery_locking.py",
    "recovery_handoff.py",
    "recovery_bootstrap.py",
}


@dataclass(frozen=True)
class HandoffResult:
    ok: bool
    state: str
    detail: str = ""
    helper_spawned: bool = False
    guard_release_attempted: bool = False
    guard_released: bool = False
    cleared: bool = False
    journal: dict[str, Any] | None = None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bounded_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryJournalError("invalid_handoff_timeout") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise RecoveryJournalError("invalid_handoff_timeout")
    return max(1.0, min(timeout, 60.0))


def stable_bootstrap_path(root: Path) -> Path:
    """Return the active bootstrap only after verifying its exact stable bundle."""
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
    if not isinstance(manifest, dict) or int(manifest.get("schema_version") or 0) != 1:
        raise RecoveryJournalError("stable_recovery_manifest_schema")
    if str(manifest.get("generation") or "") != generation:
        raise RecoveryJournalError("stable_recovery_manifest_generation")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != STABLE_RUNTIME_FILES:
        raise RecoveryJournalError("stable_recovery_manifest_files")
    for name, expected in files.items():
        if not isinstance(name, str) or Path(name).name != name:
            raise RecoveryJournalError("stable_recovery_filename_invalid")
        path = resolved / name
        if not path.is_file() or path.is_symlink() or _sha256(path) != str(expected or "").lower():
            raise RecoveryJournalError(f"stable_recovery_hash_mismatch:{name}")
    return resolved / "recovery_bootstrap.py"


def _load_exact_validated_journal(
    root: Path,
    expected: dict[str, Any],
    *,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    try:
        from .recovery_state import load_journal, validate_journal
    except ImportError:
        from recovery_state import load_journal, validate_journal

    expected = validate_journal(expected, root=Path(root), backup_root=backup_root)
    expected_state = str(expected.get("state") or "")
    if expected_state not in VALIDATED_STATES or not bool(expected.get("recovery_required")):
        raise RecoveryJournalError(f"handoff_state_not_validated:{expected_state or 'unknown'}")
    current = load_journal(Path(root), backup_root=backup_root)
    if current is None:
        raise StaleJournalWriterError("handoff_journal_missing")
    if str(current.get("attempt_id") or "") != str(expected.get("attempt_id") or ""):
        raise StaleJournalWriterError("handoff_attempt_changed")
    if int(current.get("generation") or 0) != int(expected.get("generation") or 0):
        raise StaleJournalWriterError("handoff_generation_changed")
    if str(current.get("state") or "") != expected_state:
        raise StaleJournalWriterError("handoff_state_changed")
    if not bool(current.get("recovery_required")):
        raise RecoveryJournalError("handoff_recovery_not_required")
    return current


def spawn_handoff_helper(
    root: Path,
    attempt_id: str,
    expected_generation: int,
    expected_state: str,
    handoff_token: str,
    mode: str,
    *,
    launcher=None,
    timeout_seconds: float = 20.0,
) -> tuple[bool, str]:
    if mode not in HANDOFF_MODES:
        return False, "invalid_handoff_mode"
    if expected_state not in VALIDATED_STATES:
        return False, "invalid_handoff_state"
    if int(expected_generation or 0) <= 0 or len(str(attempt_id or "")) < 16 or len(str(handoff_token or "")) < 16:
        return False, "invalid_handoff_identity"
    try:
        bootstrap = stable_bootstrap_path(root)
        command = [
            str(select_console_python(root)),
            str(bootstrap),
            "--handoff-launch",
            "--root",
            str(Path(root)),
            "--attempt-id",
            str(attempt_id),
            "--expected-generation",
            str(int(expected_generation)),
            "--expected-state",
            expected_state,
            "--handoff-token",
            str(handoff_token),
            "--handoff-mode",
            mode,
            "--handoff-timeout",
            str(_bounded_timeout(timeout_seconds)),
        ]
        call = launcher or subprocess.Popen
        kwargs: dict[str, Any] = {
            "cwd": str(Path(root)),
            "close_fds": True,
            "creationflags": detached_hidden_creation_flags(),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
        call(command, **kwargs)
        return True, ""
    except Exception as exc:
        return False, f"handoff_spawn_failed:{type(exc).__name__}"


def wait_for_cleared_attempt(
    root: Path,
    attempt_id: str,
    expected_generation: int,
    expected_state: str,
    handoff_token: str,
    mode: str,
    *,
    timeout_seconds: float = 20.0,
) -> tuple[bool, str]:
    """Wait for one exact validated generation to become its authorized clear."""
    try:
        from .recovery_state import load_journal
    except ImportError:
        from recovery_state import load_journal

    if expected_state not in VALIDATED_STATES:
        return False, "handoff_expected_state_invalid"
    if mode not in HANDOFF_MODES:
        return False, "invalid_handoff_mode"
    expected_generation = int(expected_generation or 0)
    if expected_generation <= 0:
        return False, "handoff_expected_generation_invalid"
    try:
        deadline = time.monotonic() + _bounded_timeout(timeout_seconds)
    except RecoveryJournalError as exc:
        return False, str(exc)

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
        generation = int(journal.get("generation") or 0)
        if state == "cleared":
            if bool(journal.get("recovery_required")):
                return False, "handoff_cleared_still_requires_recovery"
            if generation != expected_generation + 1:
                return False, "handoff_cleared_generation_mismatch"
            if str(journal.get("handoff_token") or "") != str(handoff_token):
                return False, "handoff_token_mismatch"
            if str(journal.get("handoff_mode") or "") != str(mode):
                return False, "handoff_mode_mismatch"
            if str(journal.get("handoff_from_state") or "") != expected_state:
                return False, "handoff_source_state_mismatch"
            return True, "cleared"

        if state != expected_state or generation != expected_generation:
            return False, f"handoff_state_or_generation_changed:{state or 'unknown'}:{generation}"
        if not bool(journal.get("recovery_required")):
            return False, "handoff_active_state_not_quarantined"
        if time.monotonic() >= deadline:
            return False, "handoff_clear_timeout"
        time.sleep(0.05)


def _record_launch_failure(root: Path, *, attempt_id: str, generation: int, mode: str, detail: str) -> None:
    path = Path(root) / "data" / "updater_logs" / "recovery_handoff.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "attempt_id": str(attempt_id)[:96],
            "generation": int(generation),
            "mode": str(mode)[:32],
            "error": str(detail or "")[:300],
        }
        with open(path, "a", encoding="utf-8", errors="replace") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except (OSError, ValueError, TypeError):
        return


def launch_nova_after_clear(
    root: Path,
    attempt_id: str,
    expected_generation: int,
    expected_state: str,
    handoff_token: str,
    mode: str,
    *,
    timeout_seconds: float = 20.0,
    launcher=None,
) -> tuple[bool, str]:
    ok, detail = wait_for_cleared_attempt(
        root,
        attempt_id,
        expected_generation,
        expected_state,
        handoff_token,
        mode,
        timeout_seconds=timeout_seconds,
    )
    if not ok:
        return False, detail
    flag = HANDOFF_MODES.get(mode)
    if flag is None:
        return False, "invalid_handoff_mode"
    try:
        command = [str(select_gui_python(root)), str(Path(root) / "app.py"), flag]
        call = launcher or subprocess.Popen
        kwargs: dict[str, Any] = {
            "cwd": str(Path(root)),
            "close_fds": True,
            "creationflags": detached_hidden_creation_flags(),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
        call(command, **kwargs)
        return True, ""
    except Exception as exc:
        detail = f"nova_launch_failed:{type(exc).__name__}"
        _record_launch_failure(
            root,
            attempt_id=attempt_id,
            generation=expected_generation + 1,
            mode=mode,
            detail=detail,
        )
        return False, detail


def perform_validated_handoff(
    root: Path,
    journal: dict[str, Any],
    mode: str,
    *,
    release_guard: Callable[[], None],
    helper_launcher=None,
    crash_hook: Callable[[str, dict[str, Any]], None] | None = None,
    timeout_seconds: float = 20.0,
    backup_root: Path | None = None,
    token_factory: Callable[[], str] | None = None,
) -> HandoffResult:
    """Spawn waiter -> release guard -> exact CAS clear. Never launches Nova directly."""
    if mode not in HANDOFF_MODES:
        return HandoffResult(False, str(journal.get("state") or "unknown"), "invalid_handoff_mode")
    if not callable(release_guard):
        return HandoffResult(False, str(journal.get("state") or "unknown"), "handoff_guard_releaser_required")
    try:
        expected = _load_exact_validated_journal(root, journal, backup_root=backup_root)
    except RecoveryJournalError as exc:
        return HandoffResult(False, str(journal.get("state") or "invalid"), str(exc))

    attempt_id = str(expected["attempt_id"])
    generation = int(expected["generation"])
    state = str(expected["state"])
    token = str((token_factory or (lambda: secrets.token_hex(16)))())
    if len(token) < 16:
        return HandoffResult(False, state, "invalid_handoff_token")

    spawned, spawn_detail = spawn_handoff_helper(
        root,
        attempt_id,
        generation,
        state,
        token,
        mode,
        launcher=helper_launcher,
        timeout_seconds=timeout_seconds,
    )
    if not spawned:
        return HandoffResult(False, state, spawn_detail, helper_spawned=False, journal=expected)

    payload = {
        "attempt_id": attempt_id,
        "generation": generation,
        "state": state,
        "mode": mode,
    }
    if crash_hook is not None:
        crash_hook("after_handoff_spawn_before_clear", payload)

    try:
        release_guard()
    except Exception as exc:
        return HandoffResult(
            False,
            state,
            f"runtime_guard_release_failed:{type(exc).__name__}",
            helper_spawned=True,
            guard_release_attempted=True,
            guard_released=False,
            journal=expected,
        )

    try:
        from .recovery_state import transition_journal
    except ImportError:
        from recovery_state import transition_journal
    try:
        cleared = transition_journal(
            Path(root),
            expected,
            "cleared",
            backup_root=backup_root,
            handoff_token=token,
            handoff_mode=mode,
            handoff_from_state=state,
            handoff_expected_generation=generation,
        )
    except RecoveryJournalError as exc:
        return HandoffResult(
            False,
            state,
            f"handoff_clear_failed:{type(exc).__name__}:{exc}",
            helper_spawned=True,
            guard_release_attempted=True,
            guard_released=True,
            journal=expected,
        )

    if crash_hook is not None:
        crash_hook(
            "after_handoff_clear",
            {
                "attempt_id": attempt_id,
                "generation": int(cleared["generation"]),
                "state": "cleared",
                "mode": mode,
            },
        )
    return HandoffResult(
        True,
        "cleared",
        "handoff_cleared",
        helper_spawned=True,
        guard_release_attempted=True,
        guard_released=True,
        cleared=True,
        journal=cleared,
    )
