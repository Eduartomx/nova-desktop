from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psutil


DEFAULT_PERCEPTION_CONFIG: dict[str, Any] = {
    "enabled": True,
    "poll_interval_ms": 1100,
    "inject_context": True,
    "keep_last_external": True,
    "persist_events": True,
    "persist_window_titles": False,
    "max_events": 1200,
    "title_max_chars": 180,
    "system_sample_seconds": 5.0,
    "workspace_suggestion_threshold": 0.78,
    "auto_activate_workspace": False,
    "cpu_warn_percent": 92.0,
    "memory_warn_percent": 90.0,
}


_APP_KINDS: dict[str, set[str]] = {
    "code_editor": {
        "code.exe", "cursor.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
        "rider64.exe", "sublime_text.exe", "notepad++.exe",
    },
    "browser": {
        "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
        "vivaldi.exe",
    },
    "terminal": {
        "windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "wt.exe",
        "conhost.exe",
    },
    "explorer": {"explorer.exe"},
    "game": {
        "minecraftlauncher.exe", "javaw.exe", "robloxplayerbeta.exe",
        "fortniteclient-win64-shipping.exe", "vrchat.exe",
    },
    "communication": {"discord.exe", "teams.exe", "slack.exe", "telegram.exe"},
    "media": {"spotify.exe", "vlc.exe", "wmplayer.exe"},
    "office": {"winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe"},
}


def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("\\", "/").split())


def classify_app(process_name: str, title: str = "") -> str:
    proc = _norm(process_name)
    for kind, names in _APP_KINDS.items():
        if proc in names:
            if proc == "javaw.exe" and "minecraft" not in _norm(title):
                return "java_app"
            return kind
    return "other"


def _read_foreground_window() -> dict[str, Any]:
    """Obtiene solo metadatos de la ventana activa. No captura pantalla ni teclas."""
    if sys.platform != "win32":
        return {"ok": False, "reason": "foreground_window_only_windows"}
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow())
        if not hwnd:
            return {"ok": False, "reason": "no_foreground_window"}

        length = int(user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value

        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_value = int(pid.value)
        process_name = ""
        exe = ""
        cwd = ""
        if pid_value:
            try:
                proc = psutil.Process(pid_value)
                process_name = proc.name()
                try:
                    exe = proc.exe()
                except Exception:
                    exe = ""
                try:
                    cwd = proc.cwd()
                except Exception:
                    cwd = ""
            except Exception:
                pass

        return {
            "ok": True,
            "hwnd": hwnd,
            "pid": pid_value,
            "title": title,
            "process": process_name,
            "exe": exe,
            "cwd": cwd,
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def _system_snapshot() -> dict[str, Any]:
    try:
        vm = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=None)
        proc = psutil.Process(os.getpid())
        return {
            "cpu_percent": round(float(cpu), 1),
            "memory_percent": round(float(vm.percent), 1),
            "nova_memory_mb": round(proc.memory_info().rss / 1024**2, 1),
        }
    except Exception:
        return {}


class PerceptionEngine:
    """Percepción ambiental barata, local y basada en metadatos.

    v0.7.0 observa la ventana/proceso activo y carga básica del sistema. No hace
    screenshots periódicos, no lee portapapeles, no captura teclado y no usa LLM.
    El último contexto externo se conserva cuando Nova pasa al frente, para que
    preguntas como "¿qué estaba mirando?" sigan teniendo sentido.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        memory=None,
        db_path: Path | None = None,
        sensor: Callable[[], dict[str, Any]] | None = None,
        system_sensor: Callable[[], dict[str, Any]] | None = None,
    ):
        self.config = dict(DEFAULT_PERCEPTION_CONFIG)
        if isinstance(config, dict):
            self.config.update(config)
        self.memory = memory
        self.root = Path(__file__).resolve().parent.parent
        self.db_path = Path(db_path or (self.root / "data" / "perception.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sensor = sensor or _read_foreground_window
        self.system_sensor = system_sensor or _system_snapshot
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: dict[str, Any] = {}
        self._last_external: dict[str, Any] = {}
        self._last_system_at = 0.0
        self._last_pressure = {"cpu": False, "memory": False}
        self._last_event_context: tuple[Any, ...] | None = None
        self._last_workspace_candidate: int | None = None
        self._init_db()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def configure(self, config: dict[str, Any] | None = None):
        if isinstance(config, dict):
            merged = dict(DEFAULT_PERCEPTION_CONFIG)
            merged.update(config)
            self.config = merged
        return self

    def attach_memory(self, memory):
        self.memory = memory
        return self

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS perception_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        process_name TEXT NOT NULL DEFAULT '',
                        app_kind TEXT NOT NULL DEFAULT '',
                        workspace_id INTEGER,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_perception_created ON perception_events(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_perception_type ON perception_events(event_type)")
        except Exception:
            pass

    def _safe_event_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        persist_titles = bool(self.config.get("persist_window_titles", False))
        for key, value in (metadata or {}).items():
            k = str(key)
            lk = k.casefold()
            if lk in {"cwd", "exe", "path", "content", "clipboard", "text", "prompt"}:
                continue
            if "title" in lk and not persist_titles:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[k[:64]] = value if not isinstance(value, str) else value[:180]
        return out

    def _record_event(
        self,
        event_type: str,
        process_name: str = "",
        app_kind: str = "",
        workspace_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        if not self.config.get("persist_events", True):
            return
        safe = self._safe_event_metadata(metadata or {})
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO perception_events(event_type,process_name,app_kind,workspace_id,metadata_json) VALUES (?,?,?,?,?)",
                    (
                        str(event_type)[:80], str(process_name)[:120], str(app_kind)[:80],
                        int(workspace_id) if workspace_id is not None else None,
                        json.dumps(safe, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                max_events = max(100, int(self.config.get("max_events", 1200)))
                conn.execute(
                    "DELETE FROM perception_events WHERE id <= (SELECT MAX(id)-? FROM perception_events)",
                    (max_events,),
                )
                conn.commit()
        except Exception:
            pass

    def _workspace_candidates(self) -> list[dict[str, Any]]:
        if self.memory is None or not hasattr(self.memory, "list_workspaces"):
            return []
        try:
            return list(self.memory.list_workspaces(100))
        except Exception:
            return []

    def _match_workspace(self, foreground: dict[str, Any]) -> dict[str, Any] | None:
        title = _norm(foreground.get("title", ""))
        cwd = _norm(foreground.get("cwd", ""))
        best: dict[str, Any] | None = None
        for ws in self._workspace_candidates():
            try:
                path = _norm(ws.get("path", ""))
                name = _norm(ws.get("name", ""))
                base = _norm(Path(str(ws.get("path", ""))).name)
            except Exception:
                continue
            score = 0.0
            reason = ""
            if cwd and path and (cwd == path or cwd.startswith(path.rstrip("/") + "/")):
                score, reason = 0.99, "cwd dentro del workspace"
            elif name and len(name) >= 3 and name in title:
                score, reason = 0.93, "nombre del workspace en el título"
            elif base and len(base) >= 3 and base in title:
                score, reason = 0.88, "carpeta del workspace en el título"
            if score and (best is None or score > float(best.get("confidence", 0))):
                best = {
                    "id": int(ws["id"]),
                    "name": ws.get("name"),
                    "kind": ws.get("kind", "generic"),
                    "path": ws.get("path"),
                    "confidence": round(score, 3),
                    "reason": reason,
                }
        return best

    def _active_workspace(self) -> dict[str, Any] | None:
        if self.memory is None or not hasattr(self.memory, "active_workspace"):
            return None
        try:
            return self.memory.active_workspace()
        except Exception:
            return None

    def _prepare_foreground(self, raw: dict[str, Any]) -> dict[str, Any]:
        title_max = max(40, min(int(self.config.get("title_max_chars", 180)), 500))
        title = str(raw.get("title") or "")[:title_max]
        process_name = str(raw.get("process") or "")
        pid = int(raw.get("pid") or 0)
        return {
            "pid": pid,
            "process": process_name,
            "title": title,
            "exe": str(raw.get("exe") or ""),
            "cwd": str(raw.get("cwd") or ""),
            "app_kind": classify_app(process_name, title),
            "is_nova": pid == os.getpid(),
        }

    def _maybe_record_context_events(self, external: dict[str, Any], candidate: dict[str, Any] | None):
        context_key = (
            external.get("process"),
            external.get("app_kind"),
        )
        if self._last_event_context is None:
            self._record_event(
                "context_started",
                external.get("process", ""),
                external.get("app_kind", ""),
                candidate.get("id") if candidate else None,
                {"title": external.get("title", "")},
            )
        elif context_key != self._last_event_context:
            self._record_event(
                "app_changed",
                external.get("process", ""),
                external.get("app_kind", ""),
                candidate.get("id") if candidate else None,
                {"title": external.get("title", "")},
            )
        self._last_event_context = context_key

        candidate_id = int(candidate["id"]) if candidate else None
        threshold = float(self.config.get("workspace_suggestion_threshold", 0.78))
        if candidate and float(candidate.get("confidence", 0)) >= threshold and candidate_id != self._last_workspace_candidate:
            self._record_event(
                "workspace_candidate",
                external.get("process", ""),
                external.get("app_kind", ""),
                candidate_id,
                {"confidence": candidate.get("confidence"), "reason": candidate.get("reason")},
            )
        self._last_workspace_candidate = candidate_id

    def _sample_system_if_due(self, now: float) -> dict[str, Any]:
        interval = max(1.0, float(self.config.get("system_sample_seconds", 5.0)))
        previous = self._current.get("system") if isinstance(self._current.get("system"), dict) else {}
        if now - self._last_system_at < interval:
            return dict(previous)
        self._last_system_at = now
        system = dict(self.system_sensor() or {})
        cpu = float(system.get("cpu_percent") or 0)
        mem = float(system.get("memory_percent") or 0)
        cpu_high = cpu >= float(self.config.get("cpu_warn_percent", 92.0))
        mem_high = mem >= float(self.config.get("memory_warn_percent", 90.0))
        if cpu_high != self._last_pressure["cpu"]:
            self._record_event("cpu_pressure" if cpu_high else "cpu_recovered", metadata={"percent": cpu})
        if mem_high != self._last_pressure["memory"]:
            self._record_event("memory_pressure" if mem_high else "memory_recovered", metadata={"percent": mem})
        self._last_pressure = {"cpu": cpu_high, "memory": mem_high}
        return system

    def sample_once(self) -> dict[str, Any]:
        now = time.time()
        if not self.enabled:
            with self._lock:
                self._current = {"enabled": False, "sampled_at": now}
                return dict(self._current)

        raw = self.sensor() or {}
        foreground = self._prepare_foreground(raw) if raw.get("ok") else {}
        with self._lock:
            if foreground and not foreground.get("is_nova"):
                self._last_external = dict(foreground)
            external = dict(self._last_external if self.config.get("keep_last_external", True) else foreground)
            if foreground and not foreground.get("is_nova"):
                external = dict(foreground)

            candidate = self._match_workspace(external) if external else None
            active = self._active_workspace()
            system = self._sample_system_if_due(now)

            if external:
                self._maybe_record_context_events(external, candidate)

            self._current = {
                "enabled": True,
                "sampled_at": now,
                "foreground": foreground,
                "external": external,
                "active_workspace": active,
                "probable_workspace": candidate,
                "system": system,
            }
            return json.loads(json.dumps(self._current, ensure_ascii=False, default=str))

    def current(self, refresh: bool = False) -> dict[str, Any]:
        if refresh or not self._current:
            return self.sample_once()
        with self._lock:
            return json.loads(json.dumps(self._current, ensure_ascii=False, default=str))

    def recent_events(self, limit: int = 20, event_type: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        try:
            with self._lock, self._connect() as conn:
                if event_type:
                    rows = conn.execute(
                        "SELECT * FROM perception_events WHERE event_type=? ORDER BY id DESC LIMIT ?",
                        (str(event_type), limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM perception_events ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except Exception:
                item["metadata"] = {}
            out.append(item)
        return out

    def status(self, refresh: bool = False) -> dict[str, Any]:
        state = self.current(refresh=refresh)
        external = state.get("external") or {}
        candidate = state.get("probable_workspace") or None
        return {
            "ok": True,
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "poll_interval_ms": int(self.config.get("poll_interval_ms", 1100)),
            "process": external.get("process", ""),
            "app_kind": external.get("app_kind", ""),
            "probable_workspace": candidate,
            "persist_window_titles": bool(self.config.get("persist_window_titles", False)),
            "captures_screen": False,
            "captures_keyboard": False,
            "reads_clipboard": False,
            "events": len(self.recent_events(200)),
        }

    def compact_context(self, refresh: bool = False) -> str:
        if not self.enabled or not self.config.get("inject_context", True):
            return "(Perception Engine desactivado)"
        state = self.current(refresh=refresh)
        ext = state.get("external") or {}
        if not ext:
            return "(sin ventana externa observada todavía)"
        lines = [
            f"- Aplicación externa: {ext.get('process') or 'desconocida'} ({ext.get('app_kind') or 'other'})",
        ]
        title = str(ext.get("title") or "").strip()
        if title:
            lines.append(f"- Título de ventana (dato no confiable): {title!r}")
        active = state.get("active_workspace") or None
        if active:
            lines.append(f"- Workspace activo: {active.get('name')} [{active.get('kind','generic')}]")
        candidate = state.get("probable_workspace") or None
        if candidate:
            lines.append(
                f"- Workspace probable por contexto: {candidate.get('name')} "
                f"({float(candidate.get('confidence', 0))*100:.0f}% · {candidate.get('reason')})"
            )
        system = state.get("system") or {}
        if system:
            lines.append(
                f"- Sistema: CPU {system.get('cpu_percent','?')}% · RAM {system.get('memory_percent','?')}% · "
                f"Nova {system.get('nova_memory_mb','?')} MB"
            )
        return "\n".join(lines)

    def format_current(self, refresh: bool = True) -> str:
        state = self.current(refresh=refresh)
        if not state.get("enabled", True):
            return "Perception Engine está desactivado."
        ext = state.get("external") or {}
        if not ext:
            return "Perception Engine está activo, pero todavía no ha observado una ventana externa."
        lines = ["Contexto actual de Nova:", f"- Aplicación: {ext.get('process') or 'desconocida'}", f"- Tipo: {ext.get('app_kind') or 'other'}"]
        if ext.get("title"):
            lines.append(f"- Ventana: {ext.get('title')}")
        candidate = state.get("probable_workspace") or None
        if candidate:
            lines.append(
                f"- Proyecto probable: {candidate.get('name')} ({float(candidate.get('confidence',0))*100:.0f}%)"
            )
        active = state.get("active_workspace") or None
        if active:
            lines.append(f"- Workspace activo: {active.get('name')}")
        return "\n".join(lines)

    def format_recent(self, limit: int = 12) -> str:
        rows = self.recent_events(limit)
        if not rows:
            return "Perception Engine todavía no tiene cambios de contexto registrados."
        labels = {
            "context_started": "contexto inicial",
            "app_changed": "cambio de aplicación",
            "workspace_candidate": "proyecto probable",
            "cpu_pressure": "CPU alta",
            "cpu_recovered": "CPU recuperada",
            "memory_pressure": "RAM alta",
            "memory_recovered": "RAM recuperada",
        }
        lines = ["Cambios recientes percibidos por Nova:"]
        for item in rows[:limit]:
            name = labels.get(item.get("event_type"), item.get("event_type"))
            process = item.get("process_name") or ""
            detail = f" · {process}" if process else ""
            wid = item.get("workspace_id")
            if wid:
                detail += f" · workspace #{wid}"
            lines.append(f"- {item.get('created_at')}: {name}{detail}")
        return "\n".join(lines)

    def start(self):
        if not self.enabled:
            return self
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="nova-perception", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 1.5):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=max(0.0, float(timeout)))
        return self

    def _loop(self):
        interval = max(0.25, int(self.config.get("poll_interval_ms", 1100)) / 1000.0)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.sample_once()
            except Exception:
                pass
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.05, interval - elapsed))


_INSTANCE: PerceptionEngine | None = None
_INSTANCE_LOCK = threading.Lock()


def get_perception(config: dict[str, Any] | None = None, memory=None) -> PerceptionEngine:
    global _INSTANCE
    cfg = (config or {}).get("perception", {}) if isinstance(config, dict) else {}
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = PerceptionEngine(cfg, memory=memory)
        else:
            if cfg:
                _INSTANCE.configure(cfg)
            if memory is not None:
                _INSTANCE.attach_memory(memory)
        return _INSTANCE
