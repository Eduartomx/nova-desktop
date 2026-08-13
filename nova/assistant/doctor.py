from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import psutil

from .llm_performance import get_llm_performance
from .perception import get_perception
from .profiler import get_profiler
from .self_repair import SelfRepairManager


class NovaDoctor:
    """Diagnóstico determinista, reparable y sin LLM."""

    def __init__(self, config: dict[str, Any], memory=None):
        self.config = config
        self.memory = memory
        self.root = Path(__file__).resolve().parent.parent

    @staticmethod
    def _result(name: str, status: str, detail: str, **extra) -> dict[str, Any]:
        return {"name": name, "status": status, "detail": detail, **extra}

    def _python(self):
        ok = sys.version_info[:2] >= (3, 11)
        return self._result("Python", "ok" if ok else "warn", f"{sys.version.split()[0]} · {sys.executable}")

    def _core_files(self):
        required = [
            "app.py", "assistant/agent.py", "assistant/tools.py", "assistant/ui.py",
            "assistant/memory.py", "assistant/task_engine.py", "assistant/workspace.py",
            "assistant/core_runtime.py", "assistant/perception.py", "assistant/runtime_lifecycle.py",
            "assistant/tray_controller.py", "assistant/instance_lock.py", "assistant/autostart.py",
            "updater/nova_updater.py", "NOVA_VERSION.txt",
        ]
        missing = [x for x in required if not (self.root / x).exists()]
        if missing:
            return self._result("Core Nova", "error", "Faltan: " + ", ".join(missing), missing=missing)
        version = (self.root / "NOVA_VERSION.txt").read_text(encoding="utf-8", errors="ignore").strip()
        return self._result("Core Nova", "ok", f"v{version} · bootstrap consolidado presente")

    def _architecture(self):
        try:
            from .core_runtime import architecture_status
            status = architecture_status()
            if not status.get("ok"):
                missing = [k for k, v in status.get("legacy_local_contract", {}).items() if not v]
                return self._result("Arquitectura", "error", "Contrato local incompleto: " + ", ".join(missing), architecture=status)
            native = len(status.get("github_managed_native") or [])
            adapters = len(status.get("compatibility_adapters") or [])
            return self._result("Arquitectura", "ok", f"core_runtime único · {native} dominios nativos · {adapters} adaptadores legacy · sin cadena versionada", architecture=status)
        except Exception as exc:
            return self._result("Arquitectura", "warn", str(exc))

    def _dependencies(self):
        modules = ["ollama", "psutil", "PIL", "pystray", "pynput", "requests", "bs4", "numpy", "playwright", "pywinauto"]
        missing = [name for name in modules if importlib.util.find_spec(name) is None]
        if missing:
            return self._result("Dependencias", "warn", "Faltan módulos: " + ", ".join(missing), missing=missing)
        return self._result("Dependencias", "ok", f"{len(modules)}/{len(modules)} módulos base disponibles")

    def _memory(self):
        if self.memory is None:
            return self._result("Memoria", "warn", "MemoryStore no proporcionado")
        try:
            stats = self.memory.stats()
            ws = self.memory.active_workspace()
            detail = f"DB {stats['db_size_mb']} MB · {stats['memory_items']} memorias · {stats['workspaces']} workspaces · {stats['tasks']} tareas"
            if ws:
                detail += f" · activo: {ws.get('name')}"
            detail += f" · continuity {stats.get('continuity_active', 0)} activa"
            return self._result("Memoria", "ok", detail, stats=stats)
        except Exception as exc:
            return self._result("Memoria", "error", str(exc))

    def _semantic_memory(self):
        if self.memory is None or not hasattr(self.memory, "semantic_status"):
            return self._result("Semantic Memory", "warn", "Semantic Memory no disponible")
        try:
            active = self.memory.active_workspace()
            wid = int(active["id"]) if active else None
            status = self.memory.semantic_status(workspace_id=wid, refresh=True)
            if not status.get("enabled"):
                return self._result("Semantic Memory", "warn", "Desactivada en config", semantic=status)
            if not status.get("model_available"):
                return self._result("Semantic Memory", "warn", f"{status.get('detail')} · instala con: {status.get('install_command')}", semantic=status)
            return self._result("Semantic Memory", "ok", f"{status.get('model')} · {status.get('indexed', 0)}/{status.get('total_candidates', 0)} recuerdos indexados", semantic=status)
        except Exception as exc:
            return self._result("Semantic Memory", "warn", str(exc))

    def _perception(self):
        try:
            engine = get_perception(self.config, self.memory)
            status = engine.status(refresh=True)
            if not status.get("enabled"):
                return self._result("Perception Engine", "warn", "Desactivado en config", perception=status)
            process = status.get("process") or "sin ventana externa todavía"
            candidate = status.get("probable_workspace") or None
            detail = f"{'activo' if status.get('running') else 'preparado'} · {status.get('poll_interval_ms')} ms · {process} · sin screenshot/teclado/portapapeles"
            if candidate:
                detail += f" · proyecto probable: {candidate.get('name')} ({float(candidate.get('confidence',0))*100:.0f}%)"
            return self._result("Perception Engine", "ok", detail, perception=status)
        except Exception as exc:
            return self._result("Perception Engine", "warn", str(exc))

    def _ollama(self):
        host = str(self.config.get("ollama_host", "http://127.0.0.1:11434")).rstrip("/")
        model = str(self.config.get("model", "qwen3.5:4b"))
        try:
            req = urllib.request.Request(host + "/api/tags", headers={"User-Agent": "Nova-Doctor/0.9.9"})
            with urllib.request.urlopen(req, timeout=2.5) as r:
                data = json.load(r)
            names = {str(x.get("name") or x.get("model") or "") for x in data.get("models", [])}
            if model not in names:
                return self._result("Ollama", "warn", f"Conectado, pero no aparece {model}")
            return self._result("Ollama", "ok", f"Conectado · {model} disponible")
        except Exception as exc:
            return self._result("Ollama", "error", f"No responde: {exc}")

    def _gpu(self):
        smi = shutil.which("nvidia-smi")
        if not smi:
            return self._result("GPU", "warn", "nvidia-smi no disponible; puede ser normal si no usas NVIDIA")
        try:
            cp = subprocess.run([smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if cp.returncode != 0 or not cp.stdout.strip():
                return self._result("GPU", "warn", (cp.stderr or "nvidia-smi sin datos").strip())
            parts = [x.strip() for x in cp.stdout.strip().splitlines()[0].split(",")]
            if len(parts) >= 5:
                name, util, used, total, temp = parts[:5]
                return self._result("GPU", "ok", f"{name} · {util}% · VRAM {used}/{total} MB · {temp} °C")
            return self._result("GPU", "ok", cp.stdout.strip().splitlines()[0])
        except Exception as exc:
            return self._result("GPU", "warn", str(exc))

    def _system(self):
        try:
            vm = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.15)
            proc = psutil.Process(os.getpid())
            nova_mb = proc.memory_info().rss / 1024**2
            return self._result("Sistema", "ok", f"CPU {cpu:.0f}% · RAM {vm.percent:.0f}% · Nova {nova_mb:.0f} MB")
        except Exception as exc:
            return self._result("Sistema", "warn", str(exc))

    def _github(self):
        gh = shutil.which("gh")
        if not gh:
            candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
            if candidate.exists():
                gh = str(candidate)
        if not gh:
            return self._result("GitHub", "warn", "GitHub CLI no encontrado")
        try:
            auth = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=4)
            if auth.returncode != 0:
                return self._result("GitHub", "warn", "gh instalado, pero la sesión no está autenticada")
            repo = self.config.get("updater", {}).get("repository") or "Eduartomx/nova-desktop"
            cp = subprocess.run([gh, "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"], capture_output=True, text=True, timeout=5)
            latest = cp.stdout.strip() if cp.returncode == 0 else "release no consultada"
            return self._result("GitHub", "ok", f"Sesión autenticada · latest {latest}")
        except Exception as exc:
            return self._result("GitHub", "warn", str(exc))

    def _browser(self):
        enabled = bool(self.config.get("browser", {}).get("enabled", True))
        if not enabled:
            return self._result("Browser Agent", "warn", "Desactivado en config")
        if importlib.util.find_spec("playwright") is None:
            return self._result("Browser Agent", "error", "Playwright no está instalado")
        channel = self.config.get("browser", {}).get("channel", "msedge")
        return self._result("Browser Agent", "ok", f"Playwright disponible · canal {channel}")

    def _resident_runtime(self):
        try:
            from .runtime_lifecycle import get_current_lifecycle
            lifecycle = get_current_lifecycle()
            cfg = self.config.get("resident_mode", {}) if isinstance(self.config, dict) else {}
            if lifecycle is None:
                enabled = bool(cfg.get("enabled", True))
                return self._result("Resident Mode", "warn" if enabled else "ok", "runtime todavía no inicializado" if enabled else "desactivado en config")
            status = lifecycle.status()
            tray = status.get("tray") or {}
            instance = status.get("single_instance") or {}
            autostart = status.get("start_with_windows") or {}
            state = str(status.get("state") or "?")
            visible = "oculta" if status.get("window_hidden") else "visible"
            tray_text = "activa" if tray.get("available") else ("degradada" if tray.get("degraded") else "no disponible")
            instance_ok = bool(instance.get("acquired") or getattr(lifecycle.instance, "acquired", False))
            auto_text = "activo" if autostart.get("enabled") else "inactivo"
            detail = f"{state} · ventana {visible} · bandeja {tray_text} · instancia {'adquirida' if instance_ok else 'no confirmada'} · inicio Windows {auto_text}"
            reason = str(status.get("last_shutdown_reason") or "")
            if reason:
                detail += f" · último cierre: {reason}"
            errors = list(status.get("recent_errors") or [])
            if errors:
                detail += f" · {len(errors)} error(es) recientes de lifecycle"
            severity = "ok" if (not status.get("resident_enabled") or tray.get("available")) else "warn"
            return self._result("Resident Mode", severity, detail, resident=status)
        except Exception as exc:
            return self._result("Resident Mode", "warn", f"{type(exc).__name__}: {str(exc)[:220]}")

    def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        checks: list[Callable[[], dict[str, Any]]] = [
            self._python, self._core_files, self._architecture, self._dependencies,
            self._memory, self._semantic_memory, self._perception, self._ollama,
            self._gpu, self._system, self._github, self._browser, self._resident_runtime,
        ]
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="nova-doctor") as pool:
            future_map = {pool.submit(fn): fn.__name__ for fn in checks}
            for future in as_completed(future_map):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(self._result(future_map[future], "error", str(exc)))
        wanted = [
            "Python", "Core Nova", "Arquitectura", "Dependencias", "Memoria",
            "Semantic Memory", "Perception Engine", "Ollama", "GPU", "Sistema", "GitHub", "Browser Agent", "Resident Mode",
        ]
        results.sort(key=lambda x: wanted.index(x["name"]) if x["name"] in wanted else 999)
        duration = round(time.perf_counter() - started, 2)
        severity = "error" if any(x["status"] == "error" for x in results) else ("warn" if any(x["status"] == "warn" for x in results) else "ok")
        report = {"ok": severity != "error", "severity": severity, "duration_seconds": duration, "checks": results}
        try:
            report["repairs"] = SelfRepairManager(self.config, self.memory).available_actions(report)
        except Exception:
            report["repairs"] = []
        try:
            profiler = get_profiler(self.config)
            windows = profiler.windows()
            report["performance_windows"] = windows
            report["performance"] = windows.get("session") or {"ok": False, "operations": []}
        except Exception:
            report["performance"] = {"ok": False, "operations": []}
            report["performance_windows"] = {}
        try:
            llm_monitor = get_llm_performance(self.config)
            llm_windows = llm_monitor.windows()
            report["llm_performance_windows"] = llm_windows
            report["llm_performance"] = llm_windows.get("session") or {"ok": False, "calls": 0}
        except Exception:
            report["llm_performance"] = {"ok": False, "calls": 0}
            report["llm_performance_windows"] = {}
        return report

    @staticmethod
    def format_text(report: dict[str, Any]) -> str:
        icons = {"ok": "✅", "warn": "⚠️", "error": "❌"}
        lines = [f"Nova Doctor · {report.get('duration_seconds', '?')} s", ""]
        for item in report.get("checks", []):
            lines.append(f"{icons.get(item.get('status'), '•')} {item.get('name')}: {item.get('detail')}")
        errors = sum(1 for x in report.get("checks", []) if x.get("status") == "error")
        warns = sum(1 for x in report.get("checks", []) if x.get("status") == "warn")
        lines += ["", f"Resultado: {errors} errores · {warns} avisos."]
        if errors == 0:
            lines.append("No detecté un problema crítico en los componentes base de Nova.")

        repairs = list(report.get("repairs") or [])
        if repairs:
            lines += ["", "Reparaciones disponibles:"]
            lines += [f"- {item.get('title')}: {item.get('detail')}" for item in repairs[:8]]
            lines.append("Abre Nova Doctor para ejecutar una reparación con confirmación.")

        perf = report.get("performance") if isinstance(report.get("performance"), dict) else {}
        slow = list(perf.get("slow_operations") or [])
        if slow:
            lines += ["", "Rendimiento de esta sesión: " + ", ".join(f"{x.get('operation')} {x.get('avg_ms')} ms" for x in slow[:4])]

        llm = report.get("llm_performance") if isinstance(report.get("llm_performance"), dict) else {}
        if llm.get("calls"):
            lines.append("LLM esta sesión: " f"{llm.get('avg_wall_ms')} ms prom. · {llm.get('avg_eval_tps')} tok/s · " f"carga {llm.get('avg_load_ms')} ms · prompt {llm.get('avg_prompt_eval_ms')} ms · generación {llm.get('avg_eval_ms')} ms")
        return "\n".join(lines)
