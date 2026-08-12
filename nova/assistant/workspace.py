from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_MARKERS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("nova", ("assistant", "updater"), ("NOVA_VERSION.txt", "app.py")),
    ("minecraft_server", ("mods",), ("server.properties",)),
    ("python", (), ("pyproject.toml", "requirements.txt", "setup.py")),
    ("node", (), ("package.json",)),
    ("arduino", (), ("*.ino",)),
    ("godot", (), ("project.godot",)),
    ("unity", ("Assets", "ProjectSettings"), ()),
    ("visual_studio", (), ("*.sln", "*.csproj")),
    ("git", (".git",), ()),
]


def _has_glob(path: Path, pattern: str) -> bool:
    try:
        return next(path.glob(pattern), None) is not None
    except OSError:
        return False


def detect_workspace_kind(path: Path) -> tuple[str, list[str]]:
    """Detecta el tipo de proyecto usando solo marcadores locales y rápidos."""
    path = Path(path)
    if not path.is_dir():
        return "generic", []

    found: list[str] = []
    for kind, dirs, files in PROJECT_MARKERS:
        ok = True
        local_found: list[str] = []
        for dirname in dirs:
            marker = path / dirname
            if not marker.exists():
                ok = False
                break
            local_found.append(dirname + ("/" if marker.is_dir() else ""))
        if not ok:
            continue
        for filename in files:
            if "*" in filename or "?" in filename:
                if not _has_glob(path, filename):
                    ok = False
                    break
                local_found.append(filename)
            else:
                marker = path / filename
                if not marker.exists():
                    ok = False
                    break
                local_found.append(filename)
        if ok:
            return kind, local_found

    return "generic", found


def workspace_snapshot(path: Path, max_items: int = 80) -> dict[str, Any]:
    """Genera un resumen barato del workspace sin recorrer todo el disco."""
    path = Path(path).resolve()
    kind, markers = detect_workspace_kind(path)
    items: list[dict[str, Any]] = []
    try:
        children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
    except OSError:
        children = []
    for child in children[: max(1, min(int(max_items), 200))]:
        try:
            items.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        except OSError:
            continue

    metadata: dict[str, Any] = {
        "kind": kind,
        "markers": markers,
        "top_level_items": items,
    }

    if kind == "minecraft_server":
        mods_dir = path / "mods"
        if mods_dir.is_dir():
            try:
                mods = [p.name for p in mods_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jar"]
                metadata["mods_count"] = len(mods)
                metadata["mods_sample"] = sorted(mods, key=str.casefold)[:30]
            except OSError:
                pass
        props = path / "server.properties"
        if props.is_file():
            wanted = {"motd", "server-port", "max-players", "online-mode", "level-name"}
            parsed: dict[str, str] = {}
            try:
                for line in props.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() in wanted:
                        parsed[k.strip()] = v.strip()
                metadata["server_properties"] = parsed
            except OSError:
                pass

    if (path / ".git").exists():
        metadata["git_repo"] = True

    return metadata


class WorkspaceManager:
    """Capa fina de detección/operaciones de workspace sobre MemoryStore."""

    def __init__(self, memory):
        self.memory = memory

    def create(self, path: str, name: str | None = None, description: str = "") -> dict[str, Any]:
        p = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
        if not p.is_dir():
            raise ValueError(f"La carpeta no existe: {p}")
        snapshot = workspace_snapshot(p)
        workspace_id = self.memory.create_workspace(
            name=(name or p.name or str(p)).strip(),
            path=str(p),
            kind=str(snapshot.get("kind", "generic")),
            description=description.strip(),
            metadata=snapshot,
            set_active=True,
        )
        return self.memory.get_workspace(workspace_id) or {"id": workspace_id, "path": str(p)}

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.memory.list_workspaces(limit)

    def active(self) -> dict[str, Any] | None:
        return self.memory.active_workspace()

    def set_active(self, selector: str | int) -> dict[str, Any] | None:
        return self.memory.set_active_workspace(selector)

    def inspect(self, selector: str | int | None = None, refresh: bool = True) -> dict[str, Any] | None:
        ws = self.memory.resolve_workspace(selector) if selector not in (None, "") else self.memory.active_workspace()
        if not ws:
            return None
        path = Path(ws["path"])
        if refresh and path.is_dir():
            meta = workspace_snapshot(path)
            self.memory.update_workspace_metadata(int(ws["id"]), meta, kind=str(meta.get("kind", ws.get("kind") or "generic")))
            ws = self.memory.get_workspace(int(ws["id"])) or ws
        return ws

    @staticmethod
    def compact_context(workspace: dict[str, Any] | None) -> str:
        if not workspace:
            return "(sin workspace activo)"
        meta = workspace.get("metadata") or {}
        lines = [
            f"Nombre: {workspace.get('name')}",
            f"Tipo: {workspace.get('kind', 'generic')}",
            f"Ruta: {workspace.get('path')}",
        ]
        if workspace.get("description"):
            lines.append(f"Descripción: {workspace.get('description')}")
        markers = meta.get("markers") or []
        if markers:
            lines.append("Marcadores: " + ", ".join(str(x) for x in markers[:12]))
        if meta.get("mods_count") is not None:
            lines.append(f"Mods detectados: {meta.get('mods_count')}")
        props = meta.get("server_properties") or {}
        if props:
            lines.append("Servidor: " + json.dumps(props, ensure_ascii=False))
        return "\n".join(lines)
