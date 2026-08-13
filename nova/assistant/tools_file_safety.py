from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path


def install_tools_file_safety():
    from . import tools as mod

    LocalTools = mod.LocalTools
    if getattr(LocalTools, "_nova_file_safety", False):
        return mod

    original_write = LocalTools.write_file

    def write_file(self, path, content, append=False):
        backup_path = None
        try:
            target = self._ensure_allowed(self._resolve_path(path))
            security = self.config.get("security", {}) if isinstance(self.config, dict) else {}
            if target.is_file() and bool(security.get("backup_overwritten_files", True)):
                root = Path(__file__).resolve().parent.parent / "data" / "file_backups"
                root.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(str(target).encode("utf-8", errors="ignore")).hexdigest()[:12]
                stamp = time.strftime("%Y%m%d-%H%M%S")
                suffix = target.suffix[:20]
                backup_path = root / f"{stamp}-{digest}-{target.stem[:50]}{suffix}.bak"
                shutil.copy2(target, backup_path)
        except Exception as exc:
            # Si la política exige backup y no pudimos crearlo, no sobrescribimos.
            try:
                target = self._ensure_allowed(self._resolve_path(path))
                required = target.is_file() and bool(self.config.get("security", {}).get("backup_overwritten_files", True))
            except Exception:
                required = False
            if required:
                return {"ok": False, "error": "backup_failed", "detail": str(exc)[:500]}

        result = original_write(self, path, content, append=append)
        if isinstance(result, dict) and backup_path is not None:
            result = dict(result)
            result["backup"] = str(backup_path)
        return result

    LocalTools.write_file = write_file
    LocalTools._nova_file_safety = True
    return mod
