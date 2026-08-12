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
import urllib.request
import zipfile
from pathlib import Path


APP_NAME = "Nova"
UPDATER_VERSION = "1.1-github"
USER_AGENT = f"Nova-Updater/{UPDATER_VERSION}"


def find_nova_root() -> Path:
    env = os.environ.get("NOVA_HOME")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates += [
        Path.home() / "Nova",
        Path(__file__).resolve().parents[1],
    ]
    for p in candidates:
        if (p / "app.py").exists() or (p / "NOVA_VERSION.txt").exists():
            return p.resolve()
    return Path(__file__).resolve().parents[1]


ROOT = find_nova_root()
CONFIG_PATH = ROOT / "updater" / "update_config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "repository": "Eduartomx/nova-desktop",
        "channel": "stable",
        "asset_prefix": "Nova_Parche_",
        "github_api": "https://api.github.com",
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


def request_json(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get_release(cfg: dict) -> dict:
    repo = cfg["repository"]
    api = cfg.get("github_api", "https://api.github.com").rstrip("/")
    channel = cfg.get("channel", "stable").lower()

    if channel == "stable":
        return request_json(f"{api}/repos/{repo}/releases/latest")

    releases = request_json(f"{api}/repos/{repo}/releases?per_page=30")
    usable = [r for r in releases if not r.get("draft")]
    if not usable:
        raise RuntimeError("No hay releases publicadas.")
    return usable[0]


def choose_patch_asset(release: dict, cfg: dict) -> dict:
    prefix = cfg.get("asset_prefix", "Nova_Parche_")
    assets = release.get("assets") or []
    zips = [
        a for a in assets
        if str(a.get("name", "")).startswith(prefix)
        and str(a.get("name", "")).lower().endswith(".zip")
    ]
    if not zips:
        raise RuntimeError(
            f"La release {release.get('tag_name')} no contiene un ZIP {prefix}*.zip"
        )
    zips.sort(key=lambda a: a.get("updated_at") or a.get("created_at") or "", reverse=True)
    return zips[0]


def download(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_asset(path: Path, asset: dict, release: dict) -> tuple[bool, str]:
    actual = sha256(path).lower()

    digest = (asset.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1].strip()
        return actual == expected, f"GitHub digest: {expected}"

    sha_assets = [
        a for a in release.get("assets", [])
        if str(a.get("name", "")).lower().endswith(".sha256")
    ]
    for a in sha_assets:
        try:
            with tempfile.TemporaryDirectory(prefix="NovaSha_") as td:
                p = Path(td) / a["name"]
                download(a["browser_download_url"], p)
                txt = p.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"\b([a-fA-F0-9]{64})\b", txt)
                if m:
                    expected = m.group(1).lower()
                    return actual == expected, f"SHA256 asset: {expected}"
        except Exception:
            pass

    return True, "Sin digest remoto; se calculó SHA256 local: " + actual


def backup_nova() -> Path | None:
    backup_dir = ROOT / "data" / "updater_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = backup_dir / f"nova_before_{stamp}"

    selected = []
    for name in ["app.py", "config.json", "assistant", "NOVA_VERSION.txt", "updater", "INICIAR.bat"]:
        p = ROOT / name
        if p.exists():
            selected.append(p)

    if not selected:
        return None

    with tempfile.TemporaryDirectory(prefix="NovaBackup_") as td:
        stage = Path(td) / "Nova"
        stage.mkdir()
        for src in selected:
            dst = stage / src.name
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        zip_path = Path(shutil.make_archive(str(out), "zip", root_dir=stage))
    return zip_path


def safe_extract(z: zipfile.ZipFile, dest: Path):
    root = dest.resolve()
    for info in z.infolist():
        target = (dest / info.filename).resolve()
        if root not in target.parents and target != root:
            raise RuntimeError(f"Ruta insegura dentro del ZIP: {info.filename}")
    z.extractall(dest)


def find_patch_root(extracted: Path) -> Path:
    if (extracted / "patch.json").exists():
        return extracted
    items = list(extracted.iterdir())
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return extracted


def copy_payload(src: Path, dst: Path):
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def run_post_install(patch_root: Path):
    bat = patch_root / "post_install.bat"
    ps1 = patch_root / "post_install.ps1"
    if bat.exists():
        rc = subprocess.run(
            ["cmd.exe", "/d", "/c", str(bat), str(ROOT)],
            cwd=str(ROOT),
        ).returncode
        if rc != 0:
            raise RuntimeError(f"post_install.bat terminó con código {rc}")
    elif ps1.exists():
        rc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1), "-NovaRoot", str(ROOT)],
            cwd=str(ROOT),
        ).returncode
        if rc != 0:
            raise RuntimeError(f"post_install.ps1 terminó con código {rc}")


def apply_zip(zip_path: Path):
    backup = backup_nova()
    if backup:
        print(f"Backup: {backup}")

    with tempfile.TemporaryDirectory(prefix="NovaPatch_") as td:
        extracted = Path(td)
        with zipfile.ZipFile(zip_path, "r") as z:
            safe_extract(z, extracted)
        pr = find_patch_root(extracted)

        manifest = pr / "patch.json"
        payload = pr / "payload"
        if not manifest.exists() or not payload.is_dir():
            legacy = pr / "APLICAR_PARCHE.bat"
            if legacy.exists():
                print("Parche legacy detectado.")
                tmp_legacy = ROOT / "_legacy_patch_temp"
                if tmp_legacy.exists():
                    shutil.rmtree(tmp_legacy, ignore_errors=True)
                shutil.copytree(pr, tmp_legacy)
                rc = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(tmp_legacy / "APLICAR_PARCHE.bat")],
                    cwd=str(ROOT),
                ).returncode
                shutil.rmtree(tmp_legacy, ignore_errors=True)
                if rc != 0:
                    raise RuntimeError(f"Parche legacy falló con código {rc}")
                return
            raise RuntimeError("Formato de parche no reconocido.")

        meta = json.loads(manifest.read_text(encoding="utf-8"))
        print(f"Aplicando Nova {meta.get('version', '?')} - {meta.get('name', '')}")
        copy_payload(payload, ROOT)
        run_post_install(pr)


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
    ap.add_argument("zip", nargs="?", help="Aplicar un ZIP local en vez de consultar GitHub")
    ap.add_argument("--check", action="store_true", help="Solo comprobar actualizaciones")
    ap.add_argument("--yes", action="store_true", help="No pedir confirmación")
    args = ap.parse_args()

    if args.zip:
        p = Path(args.zip).expanduser().resolve()
        if not p.exists():
            print(f"[ERROR] No existe: {p}")
            return 1
        apply_zip(p)
        print("Actualización local completada.")
        return 0

    try:
        if args.check:
            return check_only()

        cfg = load_config()
        current = version_text()
        release = get_release(cfg)
        latest = str(release.get("tag_name", "")).lstrip("vV")
        notes = (release.get("body") or "").strip()

        print("=" * 55)
        print("NOVA UPDATER - GitHub Releases")
        print("=" * 55)
        print(f"Repositorio : {cfg['repository']}")
        print(f"Canal       : {cfg.get('channel', 'stable')}")
        print(f"Instalada   : {current}")
        print(f"Disponible  : {latest}")
        print()

        if version_key(latest) <= version_key(current):
            print("Nova ya está actualizada.")
            return 0

        if notes:
            print("Cambios:")
            print(notes[:2000])
            if len(notes) > 2000:
                print("...")
            print()

        if not args.yes:
            ans = input(f"¿Actualizar Nova {current} -> {latest}? (S/n): ").strip().lower()
            if ans in {"n", "no"}:
                return 0

        asset = choose_patch_asset(release, cfg)
        with tempfile.TemporaryDirectory(prefix="NovaDownload_") as td:
            patch = Path(td) / asset["name"]
            print(f"Descargando {asset['name']}...")
            download(asset["browser_download_url"], patch)

            ok, detail = verify_asset(patch, asset, release)
            print("Verificación:", detail)
            if not ok:
                raise RuntimeError("El SHA-256 descargado no coincide con la release.")

            apply_zip(patch)

        print()
        print("=" * 55)
        print(f"NOVA {latest} INSTALADA CORRECTAMENTE")
        print("=" * 55)

        start = input("¿Iniciar Nova ahora? (S/n): ").strip().lower()
        if start not in {"n", "no"}:
            bat = ROOT / "INICIAR.bat"
            if bat.exists():
                os.startfile(str(bat))
        return 0

    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[ERROR] GitHub todavía no tiene una Release disponible.")
            print("Ejecuta CONFIGURAR_GITHUB_NOVA.cmd para crear/configurar el repositorio.")
        else:
            print(f"[ERROR HTTP] {e}")
        return 2
    except Exception as e:
        print(f"[ERROR] {e}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
