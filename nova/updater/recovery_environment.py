from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from typing import Any, Iterable

try:
    from .recovery_journal import (
        CRITICAL_IMPORTS_BY_DISTRIBUTION, RecoveryJournalError,
        _atomic_bytes, _atomic_json, _json_bytes, _utc_now,
        recovery_runtime_root, safe_rel, validate_journal,
    )
    from .recovery_files import _reject_symlink_chain, validate_backup_path
except ImportError:
    from recovery_journal import (
        CRITICAL_IMPORTS_BY_DISTRIBUTION, RecoveryJournalError,
        _atomic_bytes, _atomic_json, _json_bytes, _utc_now,
        recovery_runtime_root, safe_rel, validate_journal,
    )
    from recovery_files import _reject_symlink_chain, validate_backup_path

def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "").strip()).lower()


def _requirements_sha256(root: Path) -> str:
    path = Path(root) / "requirements.txt"
    if not path.is_file() or path.is_symlink():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_distributions(distributions: Iterable[Any] | None = None) -> dict[str, str]:
    rows = distributions if distributions is not None else importlib.metadata.distributions()
    result: dict[str, str] = {}
    for dist in rows:
        try:
            raw_name = dist.metadata.get("Name") if hasattr(dist, "metadata") else None
            name = _normalized_distribution_name(raw_name or getattr(dist, "name", ""))
            version = str(getattr(dist, "version", "") or "")
        except Exception:
            continue
        if name and version:
            result[name] = version
    return dict(sorted(result.items()))


def capture_dependency_snapshot(
    root: Path,
    backup: Path,
    *,
    backup_root: Path | None = None,
    distributions: Iterable[Any] | None = None,
) -> tuple[str, str]:
    backup = validate_backup_path(root, backup, backup_root=backup_root)
    installed = _installed_distributions(distributions)
    critical = sorted(
        module for name, module in CRITICAL_IMPORTS_BY_DISTRIBUTION.items() if name in installed
    )
    payload = {
        "schema_version": 1,
        "created_at": _utc_now(),
        "requirements_sha256": _requirements_sha256(root),
        "distributions": installed,
        "critical_imports": critical,
    }
    data = _json_bytes(payload)
    rel = "control/dependency_snapshot.json"
    target = backup / safe_rel(rel)
    _atomic_bytes(target, data)
    return rel, hashlib.sha256(data).hexdigest()


def _load_dependency_snapshot(backup: Path, journal: dict[str, Any]) -> dict[str, Any]:
    rel = str(journal.get("dependency_snapshot_path") or "")
    expected = str(journal.get("dependency_snapshot_sha256") or "").lower()
    if not rel or not expected:
        raise RecoveryJournalError("dependency_snapshot_missing")
    path = Path(backup) / safe_rel(rel)
    _reject_symlink_chain(Path(backup), path)
    if not path.is_file() or path.is_symlink():
        raise RecoveryJournalError("dependency_snapshot_invalid")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RecoveryJournalError("dependency_snapshot_hash_mismatch")
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RecoveryJournalError(f"dependency_snapshot_corrupt:{type(exc).__name__}") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise RecoveryJournalError("dependency_snapshot_schema_invalid")
    distributions = payload.get("distributions")
    imports = payload.get("critical_imports")
    if not isinstance(distributions, dict) or not isinstance(imports, list):
        raise RecoveryJournalError("dependency_snapshot_payload_invalid")
    normalized: dict[str, str] = {}
    for name, version in distributions.items():
        key = _normalized_distribution_name(name)
        if not key or not str(version or ""):
            raise RecoveryJournalError("dependency_snapshot_distribution_invalid")
        normalized[key] = str(version)
    payload["distributions"] = dict(sorted(normalized.items()))
    payload["critical_imports"] = [str(x) for x in imports if str(x).strip()][:64]
    return payload


def _validate_python_files(root: Path) -> tuple[bool, str]:
    root = Path(root)
    for path in (root / "app.py", root / "updater" / "nova_updater.py", root / "updater" / "update_runner.py"):
        if not path.is_file() or path.is_symlink():
            return False, f"required_file_invalid:{path.name}"
    targets = [root / "app.py"]
    for directory in (root / "assistant", root / "updater"):
        if directory.is_dir() and not directory.is_symlink():
            targets.extend(sorted(directory.glob("*.py")))
    for path in targets:
        try:
            if path.is_symlink():
                return False, f"python_symlink_rejected:{path.name}"
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:
            return False, f"python_validation_failed:{path.name}:{type(exc).__name__}"
    managed = root / "updater" / "managed_files.json"
    try:
        if managed.is_symlink():
            return False, "managed_files_symlink_rejected"
        if managed.exists():
            data = json.loads(managed.read_text(encoding="utf-8"))
            for rel in data.get("files", []):
                safe_rel(str(rel))
    except Exception as exc:
        return False, f"managed_files_validation_failed:{type(exc).__name__}"
    return True, "restored_files_validated"


def _run_critical_imports(modules: list[str], *, python_executable: str | Path | None = None, timeout: float = 20.0, runner=None) -> tuple[bool, str]:
    modules = [str(x) for x in modules if str(x).strip()]
    if not modules:
        return True, "critical_imports_not_required"
    exe = str(python_executable or sys.executable)
    code = "import importlib\nmods=" + repr(modules) + "\nfor name in mods: importlib.import_module(name)\n"
    try:
        call = runner or subprocess.run
        result = call(
            [exe, "-I", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1.0, float(timeout)),
            shell=False,
        )
    except Exception as exc:
        return False, f"critical_import_exception:{type(exc).__name__}"
    if int(getattr(result, "returncode", 1)) != 0:
        return False, "critical_import_failed"
    return True, "critical_imports_validated"


def validate_restored_install(
    root: Path,
    journal: dict[str, Any] | None = None,
    backup: Path | None = None,
    *,
    distributions: Iterable[Any] | None = None,
    python_executable: str | Path | None = None,
    import_runner=None,
) -> tuple[bool, str]:
    ok, detail = _validate_python_files(root)
    if not ok:
        return ok, detail
    if journal is None or not bool(journal.get("dependencies_may_have_changed")):
        return True, detail + ";dependency_snapshot_not_required"
    if backup is None:
        return False, "dependency_snapshot_backup_required"
    try:
        snapshot = _load_dependency_snapshot(Path(backup), validate_journal(journal, root=root))
    except RecoveryJournalError as exc:
        return False, str(exc)
    expected_req = str(snapshot.get("requirements_sha256") or "")
    if expected_req != _requirements_sha256(root):
        return False, "dependency_requirements_hash_mismatch"
    expected = snapshot["distributions"]
    current = _installed_distributions(distributions)
    expected_names, current_names = set(expected), set(current)
    added = sorted(current_names - expected_names)
    removed = sorted(expected_names - current_names)
    changed = sorted(name for name in expected_names & current_names if expected[name] != current[name])
    if added:
        return False, "dependency_added:" + ",".join(added[:12])
    if removed:
        return False, "dependency_removed:" + ",".join(removed[:12])
    if changed:
        return False, "dependency_version_changed:" + ",".join(
            f"{name}:{expected[name]}->{current[name]}" for name in changed[:12]
        )
    imports_ok, imports_detail = _run_critical_imports(
        list(snapshot.get("critical_imports") or []),
        python_executable=python_executable,
        runner=import_runner,
    )
    if not imports_ok:
        return False, imports_detail
    return True, "restored_files_and_dependency_snapshot_validated"


def prepare_stable_recovery_runtime(root: Path) -> dict[str, Any]:
    """Publish a validated, immutable stdlib-only recovery bundle under data/."""
    root = Path(root)
    source_dir = root / "updater"
    runtime = recovery_runtime_root(root)
    generations = runtime / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    target = generations / generation
    target.mkdir(parents=False, exist_ok=False)
    files: dict[str, str] = {}
    for name in (
        "recovery_journal.py", "recovery_attempts.py", "recovery_files.py",
        "recovery_environment.py", "recovery_state.py", "recovery_locking.py",
        "recovery_bootstrap.py",
    ):
        src = source_dir / name
        if not src.is_file() or src.is_symlink():
            raise RecoveryJournalError(f"stable_bootstrap_source_invalid:{name}")
        data = src.read_bytes()
        dst = target / name
        _atomic_bytes(dst, data)
        files[name] = hashlib.sha256(data).hexdigest()
    manifest = {
        "schema_version": 1,
        "generation": generation,
        "created_at": _utc_now(),
        "files": files,
    }
    _atomic_json(target / "manifest.json", manifest)
    for name, expected in files.items():
        if hashlib.sha256((target / name).read_bytes()).hexdigest() != expected:
            raise RecoveryJournalError(f"stable_bootstrap_verify_failed:{name}")
    _atomic_json(runtime / "active.json", {
        "schema_version": 1,
        "generation": generation,
        "manifest_sha256": hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest(),
    })
    return manifest
