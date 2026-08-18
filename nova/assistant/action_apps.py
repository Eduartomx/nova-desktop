from __future__ import annotations

"""Fail-closed application and document target classification.

``open_app`` accepts logical aliases only.  It never treats an arbitrary path
or command as an application.  Documents use a separate capability and a
small extension allowlist; executable content remains forbidden.
"""

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlsplit


EXECUTABLE_SUFFIXES = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".msi", ".msp", ".scr", ".pif", ".cpl", ".lnk", ".url",
}
DOCUMENT_SUFFIXES = {
    ".txt", ".md", ".log", ".json", ".csv", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".docx", ".xlsx", ".pptx",
}


@dataclass(frozen=True)
class AppTarget:
    allowed: bool
    kind: str
    display_name: str
    path: Path | None = None
    reason: str = ""


_ALIASES = {
    "explorador": "explorer",
    "explorer": "explorer",
    "explorador de archivos": "explorer",
    "bloc de notas": "notepad",
    "notepad": "notepad",
    "calculadora": "calculator",
    "calculator": "calculator",
    "calc": "calculator",
}


def trusted_windows_directory() -> Path | None:
    """Ask Windows for its directory; never trust PATH or a mutable cwd."""
    if os.name != "nt":
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = int(ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            return None
        root = Path(buffer.value).resolve(strict=True)
        return root if root.is_dir() else None
    except Exception:
        return None


def _system_candidates(alias: str, root: Path) -> tuple[Path, ...]:
    if alias == "explorer":
        return (root / "explorer.exe",)
    name = "notepad.exe" if alias == "notepad" else "calc.exe"
    candidates = []
    if os.environ.get("PROCESSOR_ARCHITEW6432"):
        candidates.append(root / "Sysnative" / name)
    candidates.append(root / "System32" / name)
    return tuple(candidates)


def classify_application(value: str) -> AppTarget:
    raw = str(value or "").strip()
    alias = _ALIASES.get(raw.casefold())
    if not alias:
        return AppTarget(False, "forbidden", "Aplicación no registrada", reason="open_app sólo acepta aliases explícitos.")
    return AppTarget(True, "known_application", alias)


def resolve_known_application(
    value: str,
    *,
    windows_directory: Path | None = None,
    is_file: Callable[[Path], bool] | None = None,
) -> AppTarget:
    classified = classify_application(value)
    if not classified.allowed:
        return classified
    root = Path(windows_directory) if windows_directory is not None else trusted_windows_directory()
    if root is None:
        return AppTarget(False, "forbidden", classified.display_name, reason="Directorio de Windows no verificable.")
    try:
        root = root.resolve(strict=True)
    except OSError:
        return AppTarget(False, "forbidden", classified.display_name, reason="Directorio de Windows no verificable.")
    checker = is_file or (lambda path: path.is_file())
    for candidate in _system_candidates(classified.display_name, root):
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if resolved.suffix.casefold() != ".exe" or not checker(resolved):
                continue
            return AppTarget(True, "known_application", classified.display_name, resolved, "Alias resuelto dentro del directorio de Windows.")
        except (OSError, ValueError):
            continue
    return AppTarget(False, "forbidden", classified.display_name, reason="Aplicación registrada no encontrada en una ubicación confiable.")


def classify_document(value: str) -> AppTarget:
    raw = str(value or "").strip()
    if not raw:
        return AppTarget(False, "forbidden", "Documento no válido", reason="Ruta de documento vacía.")
    if raw.startswith(("\\\\", "//")):
        return AppTarget(False, "forbidden", "Documento no válido", reason="Las rutas UNC no están permitidas.")
    scheme = urlsplit(raw).scheme.casefold()
    if scheme and not re.fullmatch(r"[a-zA-Z]", scheme):
        return AppTarget(False, "forbidden", "Documento no válido", reason="Los esquemas no se abren como documentos locales.")
    path = Path(os.path.expandvars(os.path.expanduser(raw))).resolve(strict=False)
    suffix = path.suffix.casefold()
    if suffix in EXECUTABLE_SUFFIXES or suffix not in DOCUMENT_SUFFIXES:
        return AppTarget(False, "forbidden", path.name[:180] or "Documento", reason="Tipo de archivo no permitido para apertura reversible.")
    return AppTarget(True, "document", path.name[:180] or "Documento", path, "Documento local de tipo permitido.")
