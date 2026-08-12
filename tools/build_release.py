from __future__ import annotations
import argparse
import hashlib
import shutil
import tempfile
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--source", default="release_payload")
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()

    version = args.version.lstrip("vV")
    src = Path(args.source).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    name = f"Nova_Parche_v{version.replace('.', '_')}_GITHUB"
    zip_path = out / f"{name}.zip"

    with tempfile.TemporaryDirectory(prefix="NovaBuild_") as td:
        stage = Path(td) / name
        shutil.copytree(src, stage)
        shutil.make_archive(str(zip_path.with_suffix("")), "zip",
                            root_dir=stage.parent, base_dir=stage.name)

    h = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha = out / f"{zip_path.name}.sha256"
    sha.write_text(f"{h}  {zip_path.name}\n", encoding="utf-8")
    print(zip_path)
    print(sha)

if __name__ == "__main__":
    main()
