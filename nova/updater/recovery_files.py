from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

try:
    from .recovery_journal import (
        RecoveryJournalError, TRANSACTION_CATEGORIES, backup_root_path,
        safe_rel, validate_journal,
    )
except ImportError:
    from recovery_journal import (
        RecoveryJournalError, TRANSACTION_CATEGORIES, backup_root_path,
        safe_rel, validate_journal,
    )


def _lexical_relative(base: Path, target: Path) -> Path:
    """Return target relative to base without following either path's symlinks.

    This deliberately happens before ``resolve()``. On Windows a temporary path
    may be expressed through an 8.3 alias while ``Path.resolve()`` expands only
    one side to its long form; comparing those two lexical spellings directly
    would reject a legitimate in-root backup. ``commonpath`` + ``normcase``
    keeps the containment comparison case-insensitive on Windows without
    treating symlink resolution as proof of safety.
    """
    base_text = os.path.abspath(os.fspath(base))
    target_text = os.path.abspath(os.fspath(target))
    try:
        common = os.path.commonpath([base_text, target_text])
    except ValueError as exc:
        raise RecoveryJournalError("path_outside_authorized_root") from exc
    if os.path.normcase(common) != os.path.normcase(base_text):
        raise RecoveryJournalError("path_outside_authorized_root")
    rel_text = os.path.relpath(target_text, base_text)
    if rel_text in ("", "."):
        return Path()
    rel = Path(rel_text)
    if rel.is_absolute() or ".." in rel.parts:
        raise RecoveryJournalError("path_outside_authorized_root")
    return rel


def _reject_symlink_chain(base: Path, target: Path) -> Path:
    """Reject symlink hops and return the canonical target below base."""
    raw_base = Path(base)
    if not raw_base.exists() or raw_base.is_symlink():
        raise RecoveryJournalError("authorized_root_unavailable_or_symlink")
    rel = _lexical_relative(raw_base, Path(target))
    canonical_base = raw_base.resolve(strict=True)
    current = canonical_base
    for part in rel.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise RecoveryJournalError("unsafe_symlink_in_recovery_path")
    resolved = (canonical_base / rel).resolve(strict=False)
    try:
        resolved.relative_to(canonical_base)
    except ValueError as exc:
        raise RecoveryJournalError("path_resolves_outside_authorized_root") from exc
    return resolved


def _manifest_lists(meta: dict[str, Any]) -> dict[str, list[str]]:
    if not isinstance(meta, dict) or int(meta.get("schema") or 0) != 2:
        raise RecoveryJournalError("backup_manifest_schema_invalid")
    result: dict[str, list[str]] = {}
    for key in TRANSACTION_CATEGORIES:
        rows = meta.get(key) or []
        if not isinstance(rows, list):
            raise RecoveryJournalError("backup_manifest_category_invalid")
        result[key] = sorted({safe_rel(str(x)).as_posix() for x in rows})
    return result


def validate_backup_path(root: Path, backup: Path, *, backup_root: Path | None = None) -> Path:
    raw_base = Path(backup_root) if backup_root is not None else backup_root_path(root)
    if not raw_base.is_dir() or raw_base.is_symlink():
        raise RecoveryJournalError("authorized_backup_root_unavailable")
    rel = _lexical_relative(raw_base, Path(backup))
    base = raw_base.resolve(strict=True)
    candidate = base / rel
    # Walk every component before resolving the candidate so a symlink can
    # never be used to make an external directory appear authorized.
    resolved = _reject_symlink_chain(base, candidate)
    if not resolved.exists():
        raise RecoveryJournalError("backup_missing")
    resolved = resolved.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise RecoveryJournalError("backup_resolves_outside_authorized_root") from exc
    manifest = resolved / "backup.json"
    if not resolved.is_dir() or resolved.is_symlink() or not manifest.is_file() or manifest.is_symlink():
        raise RecoveryJournalError("backup_or_manifest_invalid")
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecoveryJournalError(f"backup_manifest_corrupt:{type(exc).__name__}") from exc
    _manifest_lists(meta)
    managed = meta.get("managed_files")
    if not isinstance(managed, dict):
        raise RecoveryJournalError("managed_files_state_missing")
    safe_rel(str(managed.get("path") or ""))
    if bool(managed.get("existed")):
        safe_rel(str(managed.get("backup") or ""))
    return resolved


def resolve_backup(root: Path, payload: dict[str, Any], *, backup_root: Path | None = None) -> Path:
    journal = validate_journal(payload, root=root, backup_root=backup_root)
    base = Path(backup_root) if backup_root is not None else backup_root_path(root)
    candidate = base / safe_rel(journal["backup_path"])
    return validate_backup_path(root, candidate, backup_root=base)


def _install_target(root: Path, rel: str) -> Path:
    raw_base = Path(root)
    if not raw_base.exists() or raw_base.is_symlink():
        raise RecoveryJournalError("install_root_unavailable_or_symlink")
    path = safe_rel(rel)
    base = raw_base.resolve(strict=True)
    target = base / path
    current = base
    for part in path.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise RecoveryJournalError("install_symlink_rejected")
    try:
        target.resolve(strict=False).relative_to(base)
    except ValueError as exc:
        raise RecoveryJournalError("install_target_outside_root") from exc
    return target


def _atomic_copy(src: Path, dst: Path) -> None:
    if not src.is_file() or src.is_symlink():
        raise RecoveryJournalError("unsafe_or_missing_backup_file")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".", suffix=".recovery-tmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        with open(tmp, "rb") as stream:
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def restore_backup_idempotent(root: Path, backup: Path, *, backup_root: Path | None = None, progress_hook=None) -> None:
    backup = validate_backup_path(root, backup, backup_root=backup_root)
    manifest_path = backup / "backup.json"
    meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    lists = _manifest_lists(meta)
    operations_done = 0
    for rel in lists["modified_existing"] + lists["deleted_existing"]:
        src = backup / "files" / safe_rel(rel)
        _reject_symlink_chain(backup, src)
        _atomic_copy(src, _install_target(root, rel))
        operations_done += 1
        if progress_hook is not None:
            progress_hook("restore_file", operations_done, rel)
    for rel in lists["created_new"]:
        target = _install_target(root, rel)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise RecoveryJournalError("created_target_invalid")
        target.unlink(missing_ok=True)
        operations_done += 1
        if progress_hook is not None:
            progress_hook("remove_created", operations_done, rel)
    managed = meta.get("managed_files")
    if not isinstance(managed, dict):
        raise RecoveryJournalError("managed_files_state_missing")
    target = _install_target(root, str(managed.get("path") or ""))
    if target != _install_target(root, "updater/managed_files.json"):
        raise RecoveryJournalError("managed_files_path_mismatch")
    if bool(managed.get("existed")):
        src = backup / safe_rel(str(managed.get("backup") or ""))
        _reject_symlink_chain(backup, src)
        _atomic_copy(src, target)
        operations_done += 1
        if progress_hook is not None:
            progress_hook("restore_managed", operations_done, "updater/managed_files.json")
    else:
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise RecoveryJournalError("managed_files_invalid")
        target.unlink(missing_ok=True)
        operations_done += 1
        if progress_hook is not None:
            progress_hook("remove_managed", operations_done, "updater/managed_files.json")
