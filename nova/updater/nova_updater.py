from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_NAME = "Nova"
UPDATER_VERSION = "2.0-github-native"
USER_AGENT = f"Nova-Updater/{UPDATER_VERSION}"


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
    return {
        "repository": "Eduartomx/nova-desktop",
        "channel": "stable",
        "github_api": "https://api.github.com",
        "source_dir": "nova",
    }


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
    stable = 1 if not suffix else 0
    return (major, minor, patch, stable, suffix)


def find_gh() -> str | None:
    candidates = [
        shutil.which("gh"),
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    return None


def gh_json(endpoint: str):
    gh = find_gh()
    if not gh:
        raise RuntimeError("GitHub CLI (gh) no está instalado. Ejecuta CONFIGURAR_GITHUB_NOVA.cmd.")
    p = subprocess.run(
        [gh, "api", endpoint],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
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
    prefix = f"blob {len(data)}\0".encode("utf-8")
    return hashlib.sha1(prefix + data).hexdigest()


def raw_url(repo: str, ref: str, path: str) -> str:
    owner, name = repo.split("/", 1)
    safe_path = "/".join(urllib.parse.quote(x, safe="") for x in path.split("/"))
    return f"https://raw.githubusercontent.com/{owner}/{name}/{urllib.parse.quote(ref, safe='')}/{safe_path}"


def download_bytes(repo: str, ref: str, path: str) -> bytes:
    url = raw_url(repo, ref, path)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.read()
    except Exception:
        gh = find_gh()
        if not gh:
            raise
        endpoint = f"repos/{repo}/contents/{path}?ref={urllib.parse.quote(ref, safe='')}"
        p = subprocess.run(
            [gh, "api", endpoint, "-H", "Accept: application/vnd.github.raw+json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "descarga gh falló".encode("utf-8")).decode("utf-8", errors="ignore"))
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
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise RuntimeError(f"Ruta insegura en release: {rel}")
    return p


def load_managed() -> set[str]:
    if not MANAGED_PATH.exists():
        return set()
    try:
        data = json.loads(MANAGED_PATH.read_text(encoding="utf-8"))
        return {str(x) for x in data.get("files", [])}
    except Exception:
        return set()


def write_managed(files: list[str], tag: str):
    MANAGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANAGED_PATH.write_text(
        json.dumps({"tag": tag, "files": sorted(files)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def same_file(a: Path, b: Path) -> bool:
    if not a.exists() or a.stat().st_size != b.stat().st_size:
        return False
    h1 = hashlib.sha256(a.read_bytes()).digest()
    h2 = hashlib.sha256(b.read_bytes()).digest()
    return h1 == h2


def create_backup(changed: list[str], deleted: list[str], old_version: str, new_version: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "data" / "updater_backups" / f"native_{old_version}_to_{new_version}_{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    saved = []
    for rel in sorted(set(changed + deleted)):
        src = ROOT / safe_rel(rel)
        if src.is_file():
            dst = backup / "files" / safe_rel(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            saved.append(rel)
    (backup / "backup.json").write_text(
        json.dumps({"from": old_version, "to": new_version, "saved": saved, "deleted": deleted}, indent=2),
        encoding="utf-8",
    )
    return backup


def restore_backup(backup: Path, new_files: list[str]):
    meta_path = backup / "backup.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    saved = set(meta.get("saved", []))
    for rel in new_files:
        target = ROOT / safe_rel(rel)
        if rel not in saved and target.is_file():
            try:
                target.unlink()
            except Exception:
                pass
    files_root = backup / "files"
    if files_root.exists():
        for src in files_root.rglob("*"):
            if src.is_file():
                rel = src.relative_to(files_root)
                dst = ROOT / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)


def validate_install() -> tuple[bool, str]:
    targets = [ROOT / "app.py"]
    assistant = ROOT / "assistant"
    if assistant.exists():
        targets.extend(assistant.glob("*.py"))
    updater = ROOT / "updater"
    if updater.exists():
        targets.extend(updater.glob("*.py"))
    for p in targets:
        if not p.exists():
            continue
        r = subprocess.run([sys.executable, "-m", "py_compile", str(p)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            return False, f"Sintaxis inválida en {p.name}: {r.stderr.strip()}"
    return True, "Sintaxis Python OK"


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

        new_files = [x["rel"] for x in entries]
        previous = load_managed()
        new_set = set(new_files)
        deleted = sorted(previous - new_set)
        changed = []
        for rel in new_files:
            if not same_file(ROOT / safe_rel(rel), stage / safe_rel(rel)):
                changed.append(rel)

        if not changed and not deleted:
            write_managed(new_files, tag)
            print("Los archivos ya coinciden con la release.")
            return

        backup = create_backup(changed, deleted, current, latest)
        print(f"Backup: {backup}")
        try:
            for rel in changed:
                src = stage / safe_rel(rel)
                dst = ROOT / safe_rel(rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            for rel in deleted:
                target = ROOT / safe_rel(rel)
                if target.is_file():
                    target.unlink()

            if "requirements.txt" in changed:
                req = ROOT / "requirements.txt"
                if req.exists():
                    print("Actualizando dependencias Python...")
                    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)])
                    if r.returncode != 0:
                        raise RuntimeError("pip install -r requirements.txt falló")

            ok, detail = validate_install()
            if not ok:
                raise RuntimeError(detail)
            print(detail)
            write_managed(new_files, tag)
        except Exception:
            print("La validación falló; restaurando backup...")
            restore_backup(backup, new_files)
            raise


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    try:
        if args.check:
            return check_only()
        cfg = load_config()
        current = version_text()
        release = get_release(cfg)
        latest = str(release.get("tag_name", "")).lstrip("vV")
        notes = (release.get("body") or "").strip()
        print("=" * 58)
        print("NOVA UPDATER 2.0 - GitHub Native")
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
        if not args.yes:
            ans = input(f"\n¿Actualizar Nova {current} -> {latest}? (S/n): ").strip().lower()
            if ans in {"n", "no"}:
                return 0
        sync_release(cfg, release)
        print("\n" + "=" * 58)
        print(f"NOVA {latest} INSTALADA DESDE GITHUB")
        print("=" * 58)
        if not args.yes:
            start = input("¿Iniciar Nova ahora? (S/n): ").strip().lower()
            if start not in {"n", "no"}:
                bat = ROOT / "INICIAR.bat"
                if bat.exists():
                    os.startfile(str(bat))
        return 0
    except Exception as e:
        print(f"[ERROR] {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
