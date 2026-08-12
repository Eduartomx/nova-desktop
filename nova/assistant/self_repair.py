from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class SelfRepairManager:
    """Reparaciones deterministas para problemas conocidos de Nova.

    No ejecuta ninguna reparación automáticamente: devuelve acciones disponibles
    y la UI debe pedir una confirmación explícita antes de ejecutarlas.
    """

    def __init__(self, config: dict[str, Any], memory=None):
        self.config = config
        self.memory = memory
        self.root = Path(__file__).resolve().parent.parent

    def _python(self) -> str:
        candidate = self.root / ".venv" / "Scripts" / "python.exe"
        return str(candidate if candidate.exists() else Path(sys.executable))

    @staticmethod
    def _find_gh() -> str | None:
        gh = shutil.which("gh")
        if gh:
            return gh
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
        return str(candidate) if candidate.exists() else None

    @staticmethod
    def _find_ollama() -> str | None:
        exe = shutil.which("ollama")
        if exe:
            return exe
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
        ]
        return next((str(x) for x in candidates if x.exists()), None)

    def available_actions(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(action_id: str, title: str, detail: str, risk: str = "medium"):
            if action_id in seen:
                return
            seen.add(action_id)
            actions.append({"id": action_id, "title": title, "detail": detail, "risk": risk})

        for item in report.get("checks", []):
            name = str(item.get("name") or "")
            status = str(item.get("status") or "")
            detail = str(item.get("detail") or "")
            low = detail.casefold()
            if status == "ok":
                continue

            if name == "Core Nova" and status == "error":
                add("repair_current_release", "Restaurar archivos de Nova", "Vuelve a sincronizar la Release estable actual desde GitHub y valida su sintaxis.")
            elif name == "Dependencias":
                add("install_requirements", "Reparar dependencias Python", "Ejecuta pip sobre requirements.txt dentro del entorno de Nova.")
            elif name == "Ollama":
                if "no responde" in low:
                    if self._find_ollama():
                        add("start_ollama", "Iniciar Ollama", "Inicia el servidor local de Ollama sin instalar nada.", "low")
                    else:
                        add("install_ollama", "Instalar Ollama", "Instala Ollama mediante winget.", "high")
                elif "no aparece" in low:
                    add("pull_main_model", "Instalar modelo principal", f"Descarga el modelo {self.config.get('model', 'qwen3.5:4b')} con Ollama.", "high")
            elif name == "Semantic Memory":
                sem = item.get("semantic") if isinstance(item.get("semantic"), dict) else {}
                if not sem.get("model_available", False):
                    add("pull_semantic_model", "Instalar modelo de memoria semántica", f"Descarga {sem.get('model') or self.config.get('semantic_memory', {}).get('model', 'qwen3-embedding:0.6b')}.", "high")
            elif name == "GitHub":
                if "cli no encontrado" in low:
                    add("install_github_cli", "Instalar GitHub CLI", "Instala GitHub CLI mediante winget.", "high")
                elif "no está autenticada" in low or "no esta autenticada" in low:
                    add("github_login", "Iniciar sesión en GitHub", "Abre el flujo oficial de autenticación de GitHub CLI.", "medium")
            elif name == "Browser Agent" and status == "error":
                add("install_requirements", "Reparar dependencias Python", "Instala las dependencias de requirements.txt, incluido Playwright.")
                add("install_playwright_browser", "Instalar navegador de Playwright", "Instala Chromium para el fallback del Browser Agent.", "high")

        return actions

    def _run(self, args: list[str], timeout: int = 900, new_console: bool = False) -> dict[str, Any]:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if new_console else getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started = time.perf_counter()
        try:
            cp = subprocess.run(
                args,
                cwd=str(self.root),
                text=True,
                capture_output=not new_console,
                timeout=timeout,
                creationflags=flags,
            )
            return {
                "ok": cp.returncode == 0,
                "returncode": cp.returncode,
                "stdout": (cp.stdout or "")[-4000:] if not new_console else "",
                "stderr": (cp.stderr or "")[-4000:] if not new_console else "",
                "duration_seconds": round(time.perf_counter() - started, 2),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "duration_seconds": round(time.perf_counter() - started, 2)}

    def execute(self, action_id: str) -> dict[str, Any]:
        action_id = str(action_id or "").strip()
        py = self._python()
        if action_id == "repair_current_release":
            updater = self.root / "updater" / "nova_updater.py"
            if not updater.exists():
                return {"ok": False, "error": f"No existe {updater}"}
            # Reutiliza las funciones del updater sin depender de que exista una
            # versión nueva: sync_release compara archivo por archivo y restaura
            # los gestionados que falten o estén corruptos en la Release estable.
            code = (
                "import runpy; "
                f"ns=runpy.run_path({str(updater)!r}); "
                "cfg=ns['load_config'](); rel=ns['get_release'](cfg); "
                "ns['sync_release'](cfg, rel)"
            )
            return self._run([py, "-c", code], timeout=1200)

        if action_id == "install_requirements":
            req = self.root / "requirements.txt"
            if not req.exists():
                return {"ok": False, "error": "No existe requirements.txt"}
            return self._run([py, "-m", "pip", "install", "-r", str(req)])

        if action_id == "start_ollama":
            ollama = self._find_ollama()
            if not ollama:
                return {"ok": False, "error": "No encontré ollama.exe"}
            try:
                subprocess.Popen([ollama, "serve"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.2)
                return {"ok": True, "detail": "Ollama iniciado"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if action_id == "pull_main_model":
            ollama = self._find_ollama()
            if not ollama:
                return {"ok": False, "error": "Ollama no está instalado"}
            return self._run([ollama, "pull", str(self.config.get("model", "qwen3.5:4b"))], timeout=3600)

        if action_id == "pull_semantic_model":
            ollama = self._find_ollama()
            if not ollama:
                return {"ok": False, "error": "Ollama no está instalado"}
            model = str(self.config.get("semantic_memory", {}).get("model", "qwen3-embedding:0.6b"))
            return self._run([ollama, "pull", model], timeout=3600)

        if action_id == "install_github_cli":
            winget = shutil.which("winget")
            if not winget:
                return {"ok": False, "error": "winget no está disponible"}
            return self._run([winget, "install", "--id", "GitHub.cli", "-e", "--accept-source-agreements", "--accept-package-agreements"], timeout=1200)

        if action_id == "github_login":
            gh = self._find_gh()
            if not gh:
                return {"ok": False, "error": "GitHub CLI no está instalado"}
            return self._run([gh, "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"], timeout=1200, new_console=True)

        if action_id == "install_ollama":
            winget = shutil.which("winget")
            if not winget:
                return {"ok": False, "error": "winget no está disponible"}
            return self._run([winget, "install", "--id", "Ollama.Ollama", "-e", "--accept-source-agreements", "--accept-package-agreements"], timeout=1200)

        if action_id == "install_playwright_browser":
            return self._run([py, "-m", "playwright", "install", "chromium"], timeout=1800)

        return {"ok": False, "error": f"Acción de reparación desconocida: {action_id}"}
