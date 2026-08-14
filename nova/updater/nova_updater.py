from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP_NAME = "Nova"
UPDATER_VERSION = "2.2-resident-transactional"
USER_AGENT = f"Nova-Updater/{UPDATER_VERSION}"
TRANSACTION_CATEGORIES = ("modified_existing", "deleted_existing", "created_new", "unchanged")
PIP_INSTALL_TIMEOUT_SECONDS = 15 * 60.0
PIP_INSTALL_TIMEOUT_MAX_SECONDS = 60 * 60.0
PIP_TERMINATE_GRACE_SECONDS = 10.0


def find_nova_root() -> Path:
    env = os.environ.get("NOVA_HOME")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates += [Path.home() / "Nova", Path(__file__).resolve().parents[1]]
    for p in candidates:
        if (p / "app.py").exists() or (p / "NOVA_VERSION.txt").exists():
            return p.resolve()
    return Path(__file__).resolve().parents[1]


ROOT = find_nova_root()
CONFIG_PATH = ROOT / "updater" / "update_config.json"
MANAGED_PATH = ROOT / "updater" / "managed_files.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {"repository": "Eduartomx/nova-desktop", "channel": "stable", "github_api": "https://api.github.com", "source_dir": "nova"}


def version_text() -> str:
    p = ROOT / "NOVA_VERSION.txt"
    if not p.exists():
        return "0.0.0"
    return p.read_text(encoding="utf-8", errors="ignore").strip().lstrip("vV")


def version_key(v: str):
    v = v.strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+.]?(.*))?$", v)
    if not m:
        nums = re.findall(r"\d+", v)
        nums = (nums + ["0", "0", "0"])[:3]
        return tuple(map(int, nums)) + (1, "")
    major, minor, patch = map(int, m.group(1, 2, 3))
    suffix = m.group(4) or ""
    return (major, minor, patch, 1 if not suffix else 0, suffix)


def find_gh() -> str | None:
    candidates = [shutil.which("gh"), str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return None


def gh_json(endpoint: str):
    gh = find_gh()
    if not gh:
        raise RuntimeError("GitHub CLI (gh) no está instalado. Ejecuta CONFIGURAR_GITHUB_NOVA.cmd.")
    p = subprocess.run([gh, "api", endpoint], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "gh api falló").strip())
    return json.loads(p.stdout)


def get_release(cfg: dict) -> dict:
    repo = cfg["repository"]
    channel = str(cfg.get("channel", "stable")).lower()
    if channel == "stable":
        return gh_json(f"repos/{repo}/releases/latest")
    releases = gh_json(f"repos/{repo}/releases?per_page=30")
    usable = [r for r in releases if not r.get("draft")]
    if channel == "beta":
        betas = [r for r in usable if r.get("prerelease")]
        if betas:
            return betas[0]
    if not usable:
        raise RuntimeError("No hay releases publicadas.")
    return usable[0]


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("utf-8") + data).hexdigest()


def raw_url(repo: str, ref: str, path: str) -> str:
    owner, name = repo.split("/", 1)
    safe_path = "/".join(urllib.parse.quote(x, safe="") for x in path.split("/"))
    return f"https://raw.githubusercontent.com/{owner}/{name}/{urllib.parse.quote(ref, safe='')}/{safe_path}"


def download_bytes(repo: str, ref: str, path: str) -> bytes:
    req = urllib.request.Request(raw_url(repo, ref, path), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.read()
    except Exception:
        gh = find_gh()
        if not gh:
            raise
        endpoint = f"repos/{repo}/contents/{path}?ref={urllib.parse.quote(ref, safe='')}"
        p = subprocess.run([gh, "api", endpoint, "-H", "Accept: application/vnd.github.raw+json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if p.returncode != 0:
            detail = (p.stderr or p.stdout or b"gh download failed").decode("utf-8", errors="ignore")
            raise RuntimeError(detail)
        return p.stdout


def release_tree(cfg: dict, tag: str) -> list[dict]:
    repo = cfg["repository"]
    source_dir = str(cfg.get("source_dir", "nova")).strip("/")
    ref = gh_json(f"repos/{repo}/git/ref/tags/{tag}")
    obj = ref.get("object", {})
    obj_type = obj.get("type")
    obj_sha = obj.get("sha")
    if obj_type == "tag":
        annotated = gh_json(f"repos/{repo}/git/tags/{obj_sha}")
        obj = annotated.get("object", {})
        obj_type = obj.get("type")
        obj_sha = obj.get("sha")
    if obj_type != "commit" or not obj_sha:
        raise RuntimeError(f"No pude resolver el tag {tag} a un commit.")
    commit = gh_json(f"repos/{repo}/git/commits/{obj_sha}")
    tree_sha = commit.get("tree", {}).get("sha")
    if not tree_sha:
        raise RuntimeError(f"El commit de {tag} no contiene tree SHA.")
    tree = gh_json(f"repos/{repo}/git/trees/{tree_sha}?recursive=1")
    prefix = source_dir + "/"
    files = []
    for item in tree.get("tree", []):
        path = str(item.get("path", ""))
        if item.get("type") != "blob" or not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        if not rel or rel.startswith("data/") or rel == "config.json":
            continue
        files.append({"repo_path": path, "rel": rel, "sha": item.get("sha", "")})
    if not files:
        raise RuntimeError(f"La release {tag} no contiene archivos bajo {source_dir}/")
    return files


def safe_rel(rel: str) -> Path:
    p = Path(str(rel))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise RuntimeError(f"Ruta insegura en release: {rel}")
    return p


def _normalized_rel(rel: str) -> str:
    return safe_rel(rel).as_posix()


def _atomic_replace_from(src: Path, dst: Path) -> None:
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise RuntimeError(f"Fuente de reemplazo inexistente: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".", suffix=".nova-tmp", dir=str(dst.parent))
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


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def load_managed() -> set[str]:
    if not MANAGED_PATH.exists():
        return set()
    try:
        data = json.loads(MANAGED_PATH.read_text(encoding="utf-8"))
        return {_normalized_rel(str(x)) for x in data.get("files", [])}
    except Exception:
        return set()


def write_managed(files: list[str], tag: str):
    normalized = sorted({_normalized_rel(x) for x in files})
    _atomic_write_json(MANAGED_PATH, {"tag": tag, "files": normalized})


def same_file(a: Path, b: Path) -> bool:
    if not a.is_file() or not b.is_file() or a.stat().st_size != b.stat().st_size:
        return False
    return hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()


def build_transaction(stage: Path, new_files: list[str], previous: set[str]) -> dict[str, list[str]]:
    manifest = {name: [] for name in TRANSACTION_CATEGORIES}
    normalized_new = sorted({_normalized_rel(rel) for rel in new_files})
    new_set = set(normalized_new)
    for rel in normalized_new:
        rel_path = safe_rel(rel)
        staged = Path(stage) / rel_path
        target = ROOT / rel_path
        if not staged.is_file():
            raise RuntimeError(f"Archivo staged inexistente: {rel}")
        if target.exists() and not target.is_file():
            raise RuntimeError(f"Destino administrado no es archivo: {rel}")
        if not target.exists():
            manifest["created_new"].append(rel)
        elif same_file(target, staged):
            manifest["unchanged"].append(rel)
        else:
            manifest["modified_existing"].append(rel)
    for rel in sorted({_normalized_rel(x) for x in previous} - new_set):
        target = ROOT / safe_rel(rel)
        if target.exists() and not target.is_file():
            raise RuntimeError(f"Destino administrado no es archivo: {rel}")
        if target.is_file():
            manifest["deleted_existing"].append(rel)
    return manifest


def _validated_manifest_lists(meta: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name in TRANSACTION_CATEGORIES:
        value = meta.get(name, [])
        if not isinstance(value, list):
            raise RuntimeError(f"Manifiesto de backup inválido: {name}")
        result[name] = sorted({_normalized_rel(str(rel)) for rel in value})
    return result


def create_backup(
    manifest: dict[str, list[str]],
    old_version: str,
    new_version: str,
    *,
    backup_root: Path | None = None,
) -> Path:
    lists = _validated_manifest_lists(manifest)
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000_000:09d}"
    base = Path(backup_root) if backup_root is not None else ROOT / "data" / "updater_backups"
    backup = base / f"native_{old_version}_to_{new_version}_{stamp}"
    backup.mkdir(parents=True, exist_ok=False)

    for rel in lists["modified_existing"] + lists["deleted_existing"]:
        rel_path = safe_rel(rel)
        src = ROOT / rel_path
        if not src.is_file():
            raise RuntimeError(f"No se puede respaldar archivo existente: {rel}")
        dst = backup / "files" / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    try:
        managed_rel = _normalized_rel(str(MANAGED_PATH.relative_to(ROOT)))
    except ValueError as exc:
        raise RuntimeError("managed_files.json está fuera de ROOT") from exc
    managed_state = {"path": managed_rel, "existed": bool(MANAGED_PATH.is_file()), "backup": ""}
    if MANAGED_PATH.exists() and not MANAGED_PATH.is_file():
        raise RuntimeError("managed_files.json no es un archivo")
    if MANAGED_PATH.is_file():
        managed_backup = backup / "control" / "managed_files.json"
        managed_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MANAGED_PATH, managed_backup)
        managed_state["backup"] = "control/managed_files.json"

    payload = {
        "schema": 2,
        "from": str(old_version),
        "to": str(new_version),
        **lists,
        "managed_files": managed_state,
    }
    _atomic_write_json(backup / "backup.json", payload)
    return backup


def apply_transaction(stage: Path, manifest: dict[str, list[str]]) -> None:
    lists = _validated_manifest_lists(manifest)
    for rel in lists["modified_existing"] + lists["created_new"]:
        rel_path = safe_rel(rel)
        _atomic_replace_from(Path(stage) / rel_path, ROOT / rel_path)
    for rel in lists["deleted_existing"]:
        target = ROOT / safe_rel(rel)
        if target.exists() and not target.is_file():
            raise RuntimeError(f"No se puede eliminar destino no-archivo: {rel}")
        if target.is_file():
            target.unlink()


def _clean_recovery_detail(detail: str) -> str:
    return str(detail or "").replace("\r", " ").replace("\n", " ").strip()[:1600]


def _dependency_recovery_message(backup: Path, *, files_rollback_ok: bool = True, detail: str = "") -> str:
    if files_rollback_ok:
        message = (
            "Los archivos administrados fueron restaurados, pero pip llegó a iniciarse y el entorno Python puede haber cambiado. "
            f"Conserva el backup en {backup} y revisa la instalación/.venv antes de reintentar. "
        )
    else:
        message = (
            "El rollback de archivos quedó incompleto y pip llegó a iniciarse, por lo que también existe incertidumbre sobre el entorno Python. "
            f"Conserva el backup en {backup} y realiza recuperación manual antes de reintentar. "
        )
    message += "Volver a ejecutar requirements.txt no garantiza eliminar paquetes adicionales ni restaurar exactamente el entorno anterior."
    clean_detail = _clean_recovery_detail(detail)
    if clean_detail:
        message += " Detalle de la actualización: " + clean_detail
    return message


def _recovery_status_path() -> Path:
    return ROOT / "data" / "update_recovery.json"


def _write_rollback_status(
    backup: Path,
    *,
    files_rollback_ok: bool,
    dependencies_may_have_changed: bool = False,
    errors: list[str] | None = None,
    recovery_detail: str = "",
) -> dict:
    recovery_required = (not bool(files_rollback_ok)) or bool(dependencies_may_have_changed)
    if dependencies_may_have_changed:
        message = _dependency_recovery_message(
            backup,
            files_rollback_ok=files_rollback_ok,
            detail=recovery_detail,
        )
    elif not files_rollback_ok:
        message = f"El rollback de archivos quedó incompleto. Conserva el backup en {backup} para recuperación manual."
        clean_detail = _clean_recovery_detail(recovery_detail)
        if clean_detail:
            message += " Detalle de la actualización: " + clean_detail
    else:
        message = "Los archivos administrados fueron restaurados correctamente."
    payload = {
        "ok": bool(files_rollback_ok),
        "files_rollback_ok": bool(files_rollback_ok),
        "dependencies_may_have_changed": bool(dependencies_may_have_changed),
        "recovery_required": bool(recovery_required),
        "errors": list(errors or []),
        "backup": str(backup),
        "message": message,
        "recovery_detail": _clean_recovery_detail(recovery_detail),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(backup / "rollback_status.json", payload)

    failure_path = ROOT / "data" / "update_rollback_failure.json"
    if files_rollback_ok:
        try:
            failure_path.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        _atomic_write_json(failure_path, payload)

    recovery_path = _recovery_status_path()
    if recovery_required:
        _atomic_write_json(recovery_path, payload)
    else:
        try:
            recovery_path.unlink(missing_ok=True)
        except OSError:
            pass
    return payload


def restore_backup(
    backup: Path,
    *,
    dependencies_may_have_changed: bool = False,
    recovery_detail: str = "",
) -> dict:
    backup = Path(backup)
    meta_path = backup / "backup.json"
    if not meta_path.is_file():
        raise RuntimeError(f"Backup sin manifiesto: {backup}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or int(meta.get("schema") or 0) != 2:
        raise RuntimeError("Manifiesto de backup incompatible")
    lists = _validated_manifest_lists(meta)
    errors: list[str] = []

    for rel in lists["modified_existing"] + lists["deleted_existing"]:
        try:
            rel_path = safe_rel(rel)
            src = backup / "files" / rel_path
            if not src.is_file():
                raise RuntimeError("copia de backup ausente")
            _atomic_replace_from(src, ROOT / rel_path)
        except Exception as exc:
            errors.append(f"restore:{rel}:{type(exc).__name__}:{exc}")

    for rel in lists["created_new"]:
        try:
            target = ROOT / safe_rel(rel)
            if target.exists() and not target.is_file():
                raise RuntimeError("destino creado ya no es archivo")
            if target.is_file():
                target.unlink()
        except Exception as exc:
            errors.append(f"remove_created:{rel}:{type(exc).__name__}:{exc}")

    managed = meta.get("managed_files")
    if not isinstance(managed, dict):
        errors.append("managed_files:missing_state")
    else:
        try:
            managed_rel = _normalized_rel(str(managed.get("path") or ""))
            target = ROOT / safe_rel(managed_rel)
            if target != MANAGED_PATH:
                raise RuntimeError("managed_files path mismatch")
            if bool(managed.get("existed")):
                backup_rel = _normalized_rel(str(managed.get("backup") or ""))
                src = backup / safe_rel(backup_rel)
                if not src.is_file():
                    raise RuntimeError("managed_files backup missing")
                _atomic_replace_from(src, MANAGED_PATH)
            else:
                if MANAGED_PATH.exists() and not MANAGED_PATH.is_file():
                    raise RuntimeError("managed_files became non-file")
                MANAGED_PATH.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"managed_files:{type(exc).__name__}:{exc}")

    status = None
    try:
        status = _write_rollback_status(
            backup,
            files_rollback_ok=not errors,
            dependencies_may_have_changed=dependencies_may_have_changed,
            errors=errors,
            recovery_detail=recovery_detail,
        )
    except Exception as exc:
        errors.append(f"rollback_status:{type(exc).__name__}:{exc}")
    if errors:
        raise RuntimeError("rollback incompleto: " + " | ".join(errors))
    return status or {
        "ok": True,
        "files_rollback_ok": True,
        "dependencies_may_have_changed": bool(dependencies_may_have_changed),
        "recovery_required": bool(dependencies_may_have_changed),
        "errors": [],
        "backup": str(backup),
        "recovery_detail": _clean_recovery_detail(recovery_detail),
    }


def validate_install() -> tuple[bool, str]:
    targets = [ROOT / "app.py"]
    assistant = ROOT / "assistant"
    updater = ROOT / "updater"
    if assistant.exists():
        targets.extend(assistant.glob("*.py"))
    if updater.exists():
        targets.extend(updater.glob("*.py"))
    for p in targets:
        if not p.exists():
            continue
        r = subprocess.run([sys.executable, "-m", "py_compile", str(p)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            return False, f"Sintaxis inválida en {p.name}: {r.stderr.strip()}"
    return True, "Sintaxis Python OK"


def _normalize_pip_timeout(timeout_seconds=None) -> float:
    if timeout_seconds is None:
        return float(PIP_INSTALL_TIMEOUT_SECONDS)
    try:
        value = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("pip timeout debe ser un número de segundos positivo") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("pip timeout debe ser un número de segundos positivo")
    return min(value, float(PIP_INSTALL_TIMEOUT_MAX_SECONDS))


def _terminate_timed_out_pip(proc) -> str:
    notes = []
    try:
        proc.terminate()
    except Exception as exc:
        notes.append(f"terminate:{type(exc).__name__}:{exc}")
    try:
        proc.wait(timeout=PIP_TERMINATE_GRACE_SECONDS)
        return " | ".join(notes)
    except subprocess.TimeoutExpired:
        notes.append("terminate_timeout")
    except Exception as exc:
        notes.append(f"wait_after_terminate:{type(exc).__name__}:{exc}")

    try:
        proc.kill()
    except Exception as exc:
        notes.append(f"kill:{type(exc).__name__}:{exc}")
    try:
        proc.wait(timeout=PIP_TERMINATE_GRACE_SECONDS)
    except Exception as exc:
        notes.append(f"wait_after_kill:{type(exc).__name__}:{exc}")
    return " | ".join(notes)


def _install_requirements(timeout_seconds=None) -> None:
    req = ROOT / "requirements.txt"
    if not req.exists():
        return
    timeout = _normalize_pip_timeout(timeout_seconds)
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    print(f"Actualizando dependencias Python (timeout {timeout:g}s)...")
    proc = subprocess.Popen(cmd)
    try:
        return_code = int(proc.wait(timeout=timeout))
    except subprocess.TimeoutExpired as exc:
        termination_detail = _terminate_timed_out_pip(proc)
        message = (
            f"pip install -r requirements.txt excedió el timeout de {timeout:g} segundos; "
            "el proceso directo de pip fue detenido y esperado. El entorno Python puede haber cambiado; "
            "se requiere recuperación antes de reintentar."
        )
        if termination_detail:
            message += " Detalle de terminación: " + termination_detail
        raise RuntimeError(message) from exc
    if return_code != 0:
        raise RuntimeError("pip install -r requirements.txt falló")


def execute_transaction(
    stage: Path,
    new_files: list[str],
    previous: set[str],
    tag: str,
    old_version: str,
    new_version: str,
    *,
    backup_root: Path | None = None,
    pip_timeout_seconds=None,
) -> tuple[Path | None, dict[str, list[str]]]:
    manifest = build_transaction(stage, new_files, previous)
    touched = manifest["modified_existing"] + manifest["deleted_existing"] + manifest["created_new"]
    if not touched:
        write_managed(new_files, tag)
        return None, manifest

    backup = create_backup(manifest, old_version, new_version, backup_root=backup_root)
    print(f"Backup: {backup}")
    dependencies_started = False
    requirements_changed = "requirements.txt" in set(manifest["modified_existing"] + manifest["created_new"])
    try:
        apply_transaction(stage, manifest)
        if requirements_changed:
            dependencies_started = True
            if pip_timeout_seconds is None:
                _install_requirements()
            else:
                _install_requirements(timeout_seconds=pip_timeout_seconds)
        ok, detail = validate_install()
        if not ok:
            raise RuntimeError(detail)
        print(detail)
        write_managed(new_files, tag)
        return backup, manifest
    except Exception as update_error:
        print("La actualización falló; restaurando transacción...")
        try:
            restore_backup(
                backup,
                dependencies_may_have_changed=dependencies_started,
                recovery_detail=str(update_error),
            )
        except Exception as rollback_error:
            raise RuntimeError(
                f"actualización falló ({update_error}); rollback incompleto ({rollback_error}); "
                f"backup conservado en {backup}"
            ) from update_error
        if dependencies_started:
            raise RuntimeError(
                f"actualización falló ({update_error}); archivos administrados restaurados; "
                + _dependency_recovery_message(backup, files_rollback_ok=True, detail=str(update_error))
            ) from update_error
        raise


def sync_release(cfg: dict, release: dict):
    repo = cfg["repository"]
    tag = str(release.get("tag_name", ""))
    if not tag:
        raise RuntimeError("Release sin tag_name.")
    latest = tag.lstrip("vV")
    current = version_text()
    entries = release_tree(cfg, tag)
    with tempfile.TemporaryDirectory(prefix="NovaNativeUpdate_") as td:
        stage = Path(td)
        print(f"Descargando {len(entries)} archivos desde {tag}...")
        for i, item in enumerate(entries, start=1):
            data = download_bytes(repo, tag, item["repo_path"])
            expected = str(item.get("sha", "")).lower()
            actual = git_blob_sha1(data)
            if expected and actual != expected:
                raise RuntimeError(f"Integridad Git incorrecta: {item['rel']}")
            dst = stage / safe_rel(item["rel"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            if i == 1 or i == len(entries) or i % 10 == 0:
                print(f"  {i}/{len(entries)}")

        new_files = [_normalized_rel(x["rel"]) for x in entries]
        previous = load_managed()
        backup, manifest = execute_transaction(stage, new_files, previous, tag, current, latest)
        if backup is None:
            print("Los archivos ya coinciden con la release.")
        else:
            changed = len(manifest["modified_existing"]) + len(manifest["created_new"]) + len(manifest["deleted_existing"])
            print(f"Transacción aplicada: {changed} archivos tocados; {len(manifest['unchanged'])} sin cambios.")


def check_only() -> int:
    cfg = load_config()
    current = version_text()
    rel = get_release(cfg)
    latest = str(rel.get("tag_name", "")).lstrip("vV")
    print(f"Instalada: {current}")
    print(f"Disponible: {latest}")
    print(f"Canal: {cfg.get('channel', 'stable')}")
    if version_key(latest) > version_key(current):
        print("UPDATE_AVAILABLE")
        return 10
    print("UP_TO_DATE")
    return 0


def _delegate_direct_update() -> int:
    runner = ROOT / "updater" / "update_runner.py"
    if not runner.exists():
        print("[ERROR] Falta updater/update_runner.py; actualización cancelada.")
        return 4
    print("Delegando la actualización al supervisor de ciclo de vida de Nova…")
    return int(subprocess.call([sys.executable, str(runner)], cwd=str(ROOT)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    try:
        if args.check:
            return check_only()
        if not args.yes:
            return _delegate_direct_update()
        cfg = load_config()
        current = version_text()
        release = get_release(cfg)
        latest = str(release.get("tag_name", "")).lstrip("vV")
        notes = (release.get("body") or "").strip()
        print("=" * 58)
        print("NOVA UPDATER 2.2 - GitHub Native · Resident-aware")
        print("=" * 58)
        print(f"Repositorio : {cfg['repository']}")
        print(f"Canal       : {cfg.get('channel', 'stable')}")
        print(f"Instalada   : {current}")
        print(f"Disponible  : {latest}")
        if version_key(latest) <= version_key(current):
            print("\nNova ya está actualizada.")
            return 0
        if notes:
            print("\nCambios:\n" + notes[:2500])
        sync_release(cfg, release)
        print("\n" + "=" * 58)
        print(f"NOVA {latest} INSTALADA DESDE GITHUB")
        print("=" * 58)
        return 0
    except Exception as e:
        print(f"[ERROR] {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
