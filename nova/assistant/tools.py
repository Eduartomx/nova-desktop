from __future__ import annotations

"""Núcleo nativo de herramientas de Nova.

v0.9.0 pone por primera vez LocalTools bajo control de GitHub. Las capas
`tools_*.py` siguen ampliando este contrato durante la migración, pero ya no
dependen de una copia histórica local que el updater no pueda reconstruir.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import psutil
import requests

from .memory import MemoryStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def _fn(name: str, description: str, properties=None, required=None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn("system_status", "Lee CPU, RAM, disco y estado básico del equipo."),
    _fn("list_processes", "Lista procesos con mayor consumo de CPU/RAM.", {"limit": {"type": "integer"}}),
    _fn("open_app", "Abre una aplicación instalada o comando conocido.", {"app": {"type": "string"}}, ["app"]),
    _fn("open_url", "Abre una URL en el navegador predeterminado.", {"url": {"type": "string"}}, ["url"]),
    _fn("web_search", "Busca información actual en Internet y devuelve resultados resumidos.", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    _fn("list_files", "Lista archivos/carpetas en una ruta permitida.", {"path": {"type": "string"}, "limit": {"type": "integer"}}),
    _fn("read_file", "Lee un archivo de texto permitido.", {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]),
    _fn("write_file", "Escribe texto en un archivo permitido. No usar para secretos.", {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}}, ["path", "content"]),
    _fn("screenshot", "Toma una captura puntual de pantalla y devuelve la ruta local."),
    _fn("clipboard_read", "Lee el portapapeles solo cuando el usuario lo solicita explícitamente."),
    _fn("clipboard_write", "Escribe texto en el portapapeles.", {"text": {"type": "string"}}, ["text"]),
    _fn("powershell", "Ejecuta PowerShell. Bloquea patrones destructivos/seguridad de alto riesgo por defecto.", {"command": {"type": "string"}, "timeout": {"type": "integer"}}, ["command"]),
    _fn("remember", "Guarda un dato estable en memoria local.", {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
]


_SIMPLE_CUES = {
    "system_status": ("cpu", "ram", "memoria ram", "estado del pc", "estado del sistema", "recursos"),
    "list_processes": ("procesos", "proceso", "consume", "consumo", "task manager"),
    "open_app": ("abre ", "abrir ", "ejecuta ", "inicia "),
    "open_url": ("url", "sitio", "pagina web", "página web"),
    "web_search": ("busca", "buscar", "internet", "web", "actual", "ultima", "última"),
    "list_files": ("archivos", "carpeta", "directorio", "lista"),
    "read_file": ("lee el archivo", "leer archivo", "contenido del archivo"),
    "write_file": ("escribe", "guarda en", "crea el archivo", "modifica el archivo"),
    "screenshot": ("captura", "screenshot", "pantalla"),
    "clipboard_read": ("portapapeles", "clipboard"),
    "clipboard_write": ("copia", "copiar", "portapapeles", "clipboard"),
    "powershell": ("powershell", "comando", "terminal"),
    "remember": ("recuerda", "memoriza", "guarda este dato"),
}


def select_tool_schemas(text: str) -> list[dict[str, Any]]:
    """Selector barato. Los adaptadores de dominio lo amplían de forma acumulativa."""
    raw = str(text or "").casefold()
    selected: list[dict[str, Any]] = []
    for schema in TOOL_SCHEMAS:
        name = str(schema.get("function", {}).get("name") or "")
        cues = _SIMPLE_CUES.get(name, ())
        if any(cue in raw for cue in cues):
            selected.append(schema)
    # El modelo necesita al menos herramientas de lectura/sistema para peticiones ambiguas,
    # pero evitamos exponer de entrada escritura/PowerShell si no hay una señal concreta.
    if not selected:
        wanted = {"system_status", "list_processes", "web_search", "read_file", "list_files"}
        selected = [x for x in TOOL_SCHEMAS if x.get("function", {}).get("name") in wanted]
    return selected


class LocalTools:
    def __init__(self, config: dict[str, Any] | None = None, memory: MemoryStore | None = None):
        self.config = config or {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.memory = memory or MemoryStore(DATA_DIR / "nova.db")

    # ---------- seguridad/rutas ----------
    def _trusted_mode(self) -> bool:
        return str(self.config.get("security", {}).get("profile", "balanced")).casefold() == "trusted"

    def _allowed_roots(self) -> list[Path]:
        roots: list[Path] = []
        values = self.config.get("security", {}).get("allowed_roots", ["~"])
        for value in values if isinstance(values, list) else [values]:
            try:
                root = Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()
                if root not in roots:
                    roots.append(root)
            except Exception:
                continue
        if not roots:
            roots.append(Path.home().resolve())
        return roots

    def _resolve_path(self, value: str | os.PathLike[str]) -> Path:
        raw = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
        if not raw:
            return Path.home().resolve()
        path = Path(raw)
        if not path.is_absolute():
            path = Path.home() / path
        return path.resolve(strict=False)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _ensure_allowed(self, path: Path) -> Path:
        resolved = Path(path).resolve(strict=False)
        restrict = bool(self.config.get("security", {}).get("restrict_files_to_allowed_roots", True))
        if not restrict:
            return resolved
        if not any(self._is_within(resolved, root) for root in self._allowed_roots()):
            raise PermissionError(f"Ruta fuera de los directorios permitidos: {resolved}")
        return resolved

    # ---------- sistema ----------
    def system_status(self):
        vm = psutil.virtual_memory()
        result: dict[str, Any] = {
            "ok": True,
            "cpu_percent": psutil.cpu_percent(interval=0.15),
            "memory_percent": float(vm.percent),
            "memory_used_gb": round((vm.total - vm.available) / (1024 ** 3), 2),
            "memory_total_gb": round(vm.total / (1024 ** 3), 2),
            "boot_time": psutil.boot_time(),
        }
        disks = []
        try:
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({"mount": part.mountpoint, "percent": usage.percent, "free_gb": round(usage.free / (1024 ** 3), 1)})
                except Exception:
                    continue
        except Exception:
            pass
        result["disks"] = disks[:12]
        # GPU NVIDIA best-effort; no falla si nvidia-smi no existe.
        try:
            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parts = [x.strip() for x in proc.stdout.splitlines()[0].split(",")]
                if len(parts) >= 4:
                    result["gpu"] = {"name": parts[0], "utilization_percent": float(parts[1]), "memory_used_mb": float(parts[2]), "memory_total_mb": float(parts[3])}
        except Exception:
            pass
        return result

    def list_processes(self, limit=18):
        limit = max(1, min(int(limit or 18), 80))
        rows = []
        for proc in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
            try:
                info = proc.info
                rows.append({
                    "pid": int(info.get("pid") or 0),
                    "name": str(info.get("name") or ""),
                    "cpu_percent": float(info.get("cpu_percent") or 0.0),
                    "memory_percent": round(float(info.get("memory_percent") or 0.0), 2),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        rows.sort(key=lambda x: (x["cpu_percent"], x["memory_percent"]), reverse=True)
        return {"ok": True, "processes": rows[:limit], "count": min(len(rows), limit)}

    # ---------- aplicaciones / web ----------
    def open_app(self, app):
        raw = str(app or "").strip()
        if not raw:
            return {"ok": False, "error": "app_required"}
        aliases = {
            "explorador": "explorer.exe", "explorer": "explorer.exe",
            "bloc de notas": "notepad.exe", "notepad": "notepad.exe",
            "calculadora": "calc.exe", "calculator": "calc.exe",
            "powershell": "powershell.exe", "cmd": "cmd.exe",
            "edge": "msedge.exe", "chrome": "chrome.exe", "vscode": "code.exe", "visual studio code": "code.exe",
        }
        target = aliases.get(raw.casefold(), raw)
        try:
            if Path(os.path.expandvars(os.path.expanduser(target))).exists():
                os.startfile(str(Path(target)))
            else:
                executable = shutil.which(target) or target
                subprocess.Popen([executable], creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            return {"ok": True, "opened": target}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

    def open_url(self, url):
        raw = str(url or "").strip()
        if not re.match(r"^https?://", raw, flags=re.I):
            raw = "https://" + raw
        try:
            ok = bool(webbrowser.open(raw, new=2))
            return {"ok": ok, "url": raw}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    def web_search(self, query, limit=6):
        query = str(query or "").strip()
        if not query:
            return {"ok": False, "error": "query_required"}
        limit = max(1, min(int(limit or 6), 10))
        timeout = float(self.config.get("internet", {}).get("timeout_seconds", 12))
        # DuckDuckGo HTML mantiene la dependencia ligera y no necesita una API key.
        try:
            from bs4 import BeautifulSoup
            response = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Nova-Desktop/0.9"},
                timeout=timeout,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            rows = []
            for result in soup.select(".result"):
                link = result.select_one(".result__a")
                if link is None:
                    continue
                snippet = result.select_one(".result__snippet")
                rows.append({
                    "title": link.get_text(" ", strip=True),
                    "url": str(link.get("href") or ""),
                    "snippet": snippet.get_text(" ", strip=True) if snippet else "",
                })
                if len(rows) >= limit:
                    break
            if rows:
                return {"ok": True, "query": query, "results": rows}
        except Exception as exc:
            # Devolvemos una URL útil en vez de inventar resultados.
            return {"ok": False, "query": query, "error": type(exc).__name__, "search_url": "https://www.google.com/search?q=" + quote_plus(query)}
        return {"ok": False, "query": query, "error": "no_results", "search_url": "https://www.google.com/search?q=" + quote_plus(query)}

    # ---------- archivos ----------
    def list_files(self, path="~", limit=100):
        p = self._ensure_allowed(self._resolve_path(path))
        if not p.is_dir():
            return {"ok": False, "error": f"No es una carpeta: {p}"}
        limit = max(1, min(int(limit or 100), 500))
        rows = []
        try:
            for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.casefold()))[:limit]:
                try:
                    stat = child.stat()
                    rows.append({"name": child.name, "path": str(child), "is_dir": child.is_dir(), "size": 0 if child.is_dir() else stat.st_size})
                except OSError:
                    continue
            return {"ok": True, "path": str(p), "items": rows}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    def read_file(self, path, max_chars=18000):
        p = self._ensure_allowed(self._resolve_path(path))
        if not p.is_file():
            return {"ok": False, "error": f"No existe el archivo: {p}"}
        max_chars = max(256, min(int(max_chars or 18000), 120000))
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            truncated = len(text) > max_chars
            return {"ok": True, "path": str(p), "content": text[:max_chars], "truncated": truncated, "size": p.stat().st_size}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    def write_file(self, path, content, append=False):
        p = self._ensure_allowed(self._resolve_path(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = "a" if bool(append) else "w"
            with p.open(mode, encoding="utf-8") as handle:
                handle.write(str(content or ""))
            return {"ok": True, "path": str(p), "bytes": len(str(content or "").encode("utf-8")), "append": bool(append)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    # ---------- captura / portapapeles ----------
    def screenshot(self):
        try:
            from PIL import ImageGrab
            folder = DATA_DIR / "screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"screenshot_{int(time.time() * 1000)}.png"
            image = ImageGrab.grab(all_screens=True)
            image.save(path)
            return {"ok": True, "path": str(path), "width": image.width, "height": image.height}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

    @staticmethod
    def _ps_clipboard(script: str, timeout: int = 4):
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def clipboard_read(self):
        try:
            proc = self._ps_clipboard("Get-Clipboard -Raw")
            return {"ok": proc.returncode == 0, "text": proc.stdout if proc.returncode == 0 else "", "error": proc.stderr.strip() if proc.returncode else None}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    def clipboard_write(self, text):
        # Pasa el valor por stdin para no incrustarlo en la línea de comandos.
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
                input=str(text or ""), capture_output=True, text=True, timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"ok": proc.returncode == 0, "error": proc.stderr.strip() if proc.returncode else None}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    # ---------- PowerShell ----------
    _DANGEROUS_PS = re.compile(
        r"(?i)(remove-item\b|del\s+/[sqf]|format-volume\b|clear-disk\b|initialize-disk\b|"
        r"stop-computer\b|restart-computer\b|shutdown\b|bcdedit\b|reg\s+delete\b|"
        r"set-executionpolicy\b|disable-windowsoptionalfeature\b|invoke-expression\b|iex\b|"
        r"downloadstring\b|frombase64string\b)"
    )

    def powershell(self, command, timeout=20):
        cmd = str(command or "").strip()
        if not cmd:
            return {"ok": False, "error": "command_required"}
        if self._DANGEROUS_PS.search(cmd):
            return {
                "ok": False,
                "error": "confirmation_required",
                "detail": "Nova Core bloqueó un patrón PowerShell destructivo/seguridad. Usa una ruta explícita con confirmación del usuario.",
            }
        timeout = max(1, min(int(timeout or 20), 120))
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-16000:],
                "stderr": proc.stderr[-8000:],
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "timeout_seconds": timeout}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}

    # ---------- memoria / dispatch ----------
    def remember(self, key, value):
        self.memory.set_memory(str(key), str(value))
        return {"ok": True, "stored": str(key)}

    def execute_tool(self, name: str, arguments: dict[str, Any] | None = None):
        method = getattr(self, str(name or ""), None)
        if not callable(method) or str(name or "").startswith("_"):
            return {"ok": False, "error": f"Herramienta desconocida: {name}"}
        try:
            return method(**dict(arguments or {}))
        except TypeError as exc:
            return {"ok": False, "error": "invalid_arguments", "detail": str(exc)[:500]}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:700]}

    # Alias histórico usado por algunas integraciones antiguas.
    execute = execute_tool
