from __future__ import annotations

"""Verifiable, sanitised Windows 11 harness for Nova v0.10.0."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable
import zipfile


FOCAL = [
    "tests.test_action_broker", "tests.test_action_policy", "tests.test_action_intent_isolation",
    "tests.test_action_app_safety", "tests.test_action_task_engine",
    "tests.test_ui_action_approval", "tests.test_action_tool_guards",
    "tests.test_repository_intelligence", "tests.test_repository_routing",
    "tests.test_v010_windows_harness",
]

MANUAL_CHECKS = [
    {
        "id": "allow_once", "title": "Permitir una vez",
        "precondition": "Tarjeta local visible para una escritura dentro de <FIXTURE>; contador de efectos en cero.",
        "steps": "Pulsa Permitir una vez y espera el resultado del mismo request_id.",
        "expected": "El contador aumenta exactamente a uno y la solicitud termina executed.",
    },
    {
        "id": "deny", "title": "Denegar",
        "precondition": "Nueva acción de fixture pendiente y archivo objetivo aún ausente.",
        "steps": "Pulsa Denegar.", "expected": "Estado denied y ningún archivo, directorio o subprocess nuevo.",
    },
    {
        "id": "close_denies", "title": "Cerrar tarjeta",
        "precondition": "Nueva tarjeta pendiente identificada.", "steps": "Cierra la tarjeta con la X.",
        "expected": "Equivale a denegar; callback y efectos permanecen en cero.",
    },
    {
        "id": "timeout", "title": "Timeout",
        "precondition": "Solicitud con timeout visible y callback aún no ejecutado.", "steps": "No respondas hasta vencer.",
        "expected": "Estado expired; no ejecución ni grant.",
    },
    {
        "id": "grant_scope", "title": "Aislamiento de grant",
        "precondition": "Grant creado en una tarea y sesión de fixture conocidas.",
        "steps": "Repite en otra tarea y luego en otra sesión.", "expected": "Ambas variantes vuelven a pedir autorización.",
    },
    {
        "id": "high_risk_reprompts", "title": "Alto riesgo one-shot",
        "precondition": "Acción high-risk permitida una vez.", "steps": "Solicita la misma acción otra vez.",
        "expected": "Aparece una nueva tarjeta; nunca existe grant de tarea.",
    },
    {
        "id": "neutral_submit", "title": "Submit con selector neutro",
        "precondition": "Página de fixture con #next type=submit y contador de envíos en cero.",
        "steps": "Solicita browser_click('#next').", "expected": "Tarjeta high-risk antes del click; un permiso produce un solo envío.",
    },
    {
        "id": "toctou", "title": "Revalidación TOCTOU",
        "precondition": "Solicitud aprobable con URL, elemento, href, formulario y archivo observados.",
        "steps": "Cambia uno de esos valores antes de consumir y repite por variante.",
        "expected": "Cada variante termina context_changed y nunca ejecuta callback.",
    },
    {
        "id": "stop_shutdown", "title": "Detener y shutdown",
        "precondition": "Worker esperando una aprobación local.", "steps": "Prueba Detener y luego cierre completo en solicitudes separadas.",
        "expected": "La espera despierta cancelled y no queda worker activo.",
    },
    {
        "id": "same_task_resume", "title": "Reanudación exacta",
        "precondition": "Task Engine detenido en un paso y request_id observados.", "steps": "Aprueba la tarjeta.",
        "expected": "Mismo task_id/paso, callback único, continuación al paso siguiente y finalización.",
    },
    {
        "id": "hidden_notification", "title": "Nova oculta",
        "precondition": "Nova de fixture oculta en bandeja y acción pendiente.", "steps": "Abre la notificación/tarjeta.",
        "expected": "La UI reaparece sin consola o pestaña adicional y permite decidir localmente.",
    },
    {
        "id": "repository_online_offline", "title": "Repository Intelligence",
        "precondition": "Fixture online; después red bloqueada manteniendo cache local.",
        "steps": "Pregunta versión, qué cambió y actividad en ambos estados.",
        "expected": "Routing sin LLM, evidencia indicada y contenido remoto nunca tratado como instrucción.",
    },
    {
        "id": "no_orphans", "title": "Cierre limpio",
        "precondition": "Se registraron runtime, wrapper, supervisor y helper de la fixture.",
        "steps": "Cierra Nova y espera la terminación controlada.",
        "expected": "Cero solicitudes, threads, ventanas, helpers, supervisores o procesos de fixture huérfanos.",
    },
]

_SECRET_KEY = re.compile(r"(?i)(password|passwd|token|secret|cookie|authorization|api[_-]?key|prompt|clipboard|content)")
_ASSIGNMENT_SECRET = re.compile(r"(?i)\b(password|token|secret|cookie|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_WINDOWS_USER_PATH = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+(?:\\[^\r\n\s]*)?")
_UNIX_PRIVATE_PATH = re.compile(r"(?:(?:/home|/Users)/[^/\s]+|/workspace/scratch/[^\s]+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_text(value: str, *, extra_secrets: Iterable[str] = ()) -> str:
    text = str(value or "")
    for secret in extra_secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _ASSIGNMENT_SECRET.sub(lambda match: match.group(1) + "=[REDACTED]", text)
    text = _WINDOWS_USER_PATH.sub("%USERPROFILE%\\[REDACTED]", text)
    text = _UNIX_PRIVATE_PATH.sub("[REDACTED_PATH]", text)
    return text[:6000]


def sanitize_value(value: Any, *, extra_secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _SECRET_KEY.search(str(key)) else sanitize_value(item, extra_secrets=extra_secrets))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, extra_secrets=extra_secrets)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return redact_text(str(value), extra_secrets=extra_secrets)


def windows_platform_info() -> dict[str, Any]:
    system = platform.system()
    version = ""
    release = platform.release()
    edition = ""
    build = 0
    try:
        win = sys.getwindowsversion()
        build = int(win.build)
        version = f"{win.major}.{win.minor}.{win.build}"
    except Exception:
        pass
    try:
        edition = str(platform.win32_edition() or "")
    except Exception:
        edition = ""
    return {"system": system, "release": release, "version": version, "edition": edition, "build": build}


def is_windows_11(info: dict[str, Any]) -> bool:
    return str(info.get("system") or "") == "Windows" and int(info.get("build") or 0) >= 22000


def current_head(root: Path) -> str:
    run = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
        timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return run.stdout.strip() if run.returncode == 0 else ""


def prepare_runtime_fixture(root: Path, destination: Path) -> Path:
    install = destination / "install"
    ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", "data", "validation-evidence")
    shutil.copytree(root / "nova", install / "nova", ignore=ignore)
    for name in ("VERSION", "requirements.txt"):
        source = root / name
        if source.is_file():
            shutil.copy2(source, install / name)
    (destination / "localappdata").mkdir(parents=True, exist_ok=True)
    return install


def launch_runtime_fixture(install: Path, fixture_root: Path):
    entry = install / "nova" / "app.py"
    if not entry.is_file():
        raise FileNotFoundError("fixture_entrypoint_missing")
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(fixture_root / "localappdata")
    env["PYTHONPATH"] = str(install / "nova")
    return subprocess.Popen(
        [sys.executable, str(entry)], cwd=str(install), env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _process_rows(process_source=None) -> tuple[list[dict[str, Any]], str]:
    if process_source is not None:
        return [dict(row) for row in process_source], "fixture_process_source"
    try:
        import psutil
        rows = []
        for process in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "create_time"]):
            try:
                row = dict(process.info)
                row["thread_count"] = int(process.num_threads())
                rows.append(row)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return rows, "psutil"
    except Exception:
        return [], "unavailable"


def _normal_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").casefold()


def _process_role(row: dict[str, Any]) -> str:
    command = " ".join(str(item) for item in (row.get("cmdline") or []))
    text = (str(row.get("name") or "") + " " + str(row.get("exe") or "") + " " + command).casefold()
    if "supervisor" in text:
        return "supervisor"
    if "handoff" in text or "helper" in text:
        return "helper"
    if ".venv" in text or "scripts\\python" in text or "scripts/python" in text:
        return "wrapper"
    return "runtime"


def _visible_window_pids(window_source=None) -> tuple[set[int], str]:
    if window_source is not None:
        values = window_source() if callable(window_source) else window_source
        return {int(value) for value in values if int(value) > 0}, "fixture_window_source"
    if os.name != "nt":
        return set(), "not_applicable"
    try:
        import ctypes
        from ctypes import wintypes
        pids: set[int] = set()
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            if ctypes.windll.user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if int(pid.value) > 0:
                    pids.add(int(pid.value))
            return True
        if not ctypes.windll.user32.EnumWindows(callback_type(callback), 0):
            return set(), "unavailable"
        return pids, "native_user32"
    except Exception:
        return set(), "unavailable"


def _owner_identity(fixture_root: Path) -> dict[str, Any]:
    candidates = (
        fixture_root / "install" / "nova" / "data" / "runtime" / "owner.json",
        fixture_root / "nova" / "data" / "runtime" / "owner.json",
        fixture_root / "data" / "runtime" / "owner.json",
    )
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > 64 * 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            keys = {str(key) for key in data} if isinstance(data, dict) else set()
            required = {"pid", "process_creation_time", "owner_id", "scope_id", "session_id", "role"}
            return {"present": True, "complete": required.issubset(keys), "schema_keys": sorted(keys & required)}
        except Exception:
            return {"present": True, "complete": False, "schema_keys": []}
    return {"present": False, "complete": False, "schema_keys": []}


def sanitized_runtime_snapshot(fixture_root: Path, *, process_source=None, thread_source=None, window_source=None) -> dict[str, Any]:
    fixture = _normal_path(Path(fixture_root).resolve(strict=False))
    rows, source = _process_rows(process_source)
    by_pid = {int(row.get("pid") or 0): row for row in rows if int(row.get("pid") or 0) > 0}
    related: set[int] = set()
    for pid, row in by_pid.items():
        command = " ".join(str(item) for item in (row.get("cmdline") or []))
        if fixture and fixture in _normal_path(str(row.get("exe") or "") + " " + command):
            related.add(pid)
    changed = True
    while changed:
        changed = False
        for pid, row in by_pid.items():
            if pid not in related and int(row.get("ppid") or 0) in related:
                related.add(pid)
                changed = True
    roles = {"runtime": 0, "wrapper": 0, "supervisor": 0, "helper": 0}
    identities = []
    parent_links = 0
    related_threads = 0
    for pid in sorted(related):
        row = by_pid[pid]
        role = _process_role(row)
        roles[role] += 1
        parent_links += int(int(row.get("ppid") or 0) in related)
        related_threads += max(0, int(row.get("thread_count") or 0))
        command = " ".join(str(item) for item in (row.get("cmdline") or []))
        fingerprint = f"{pid}|{row.get('create_time')}|{role}|{command}"
        identities.append(hashlib.sha256(fingerprint.encode("utf-8", errors="surrogatepass")).hexdigest())
    threads = list(thread_source() if callable(thread_source) else threading.enumerate())
    nova_threads = [thread for thread in threads if str(getattr(thread, "name", "")).casefold().startswith("nova-")]
    window_pids, window_source_name = _visible_window_pids(window_source)
    related_windows = len(window_pids & related)
    return {
        "thread_count": len(threads), "nova_thread_count": len(nova_threads),
        "related_thread_count": related_threads,
        "related_process_count": len(related), "roles": roles, "parent_child_links": parent_links,
        "related_window_count": related_windows, "window_observation": window_source_name,
        "identity_hashes": identities, "owner_identity": _owner_identity(Path(fixture_root)),
        "process_observation": source,
        "observation_sources": [source, "python_thread_registry", window_source_name, "fixture_path_scope", "owner.json"],
    }


def orphan_check(before: dict[str, Any], after: dict[str, Any], *, pending_requests: int = 0) -> tuple[str, str]:
    if after.get("process_observation") == "unavailable":
        return "FAIL", "process_observation_unavailable"
    clean_roles = all(int(after.get("roles", {}).get(role, 0)) <= int(before.get("roles", {}).get(role, 0)) for role in ("runtime", "wrapper", "supervisor", "helper"))
    clean_threads = int(after.get("nova_thread_count", 0)) <= int(before.get("nova_thread_count", 0))
    clean_related_threads = int(after.get("related_thread_count", 0)) <= int(before.get("related_thread_count", 0))
    clean_windows = int(after.get("related_window_count", 0)) <= int(before.get("related_window_count", 0))
    observation_ok = after.get("window_observation") != "unavailable"
    clean = clean_roles and clean_threads and clean_related_threads and clean_windows and observation_ok and int(pending_requests) == 0
    return ("PASS" if clean else "FAIL"), (
        f"roles_clean={clean_roles};threads_clean={clean_threads and clean_related_threads};"
        f"windows_clean={clean_windows and observation_ok};pending={int(pending_requests)}"
    )


def run_focal(root: Path, *, runner=subprocess.run, extra_secrets: Iterable[str] = ()) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "nova")
    started = time.monotonic()
    try:
        run = runner(
            [sys.executable, "-m", "unittest", "-v", *FOCAL], cwd=str(root), env=env,
            capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        duration = round(time.monotonic() - started, 3)
        combined = (str(getattr(run, "stdout", "") or "") + "\n" + str(getattr(run, "stderr", "") or "")).strip()
        return {
            "status": "PASS" if int(run.returncode) == 0 else "FAIL",
            "returncode": int(run.returncode), "duration_seconds": duration,
            "suites": list(FOCAL),
            "error_excerpt": "" if int(run.returncode) == 0 else redact_text(combined[-3000:], extra_secrets=extra_secrets),
        }
    except Exception as exc:
        return {
            "status": "FAIL", "returncode": -1, "duration_seconds": round(time.monotonic() - started, 3),
            "suites": list(FOCAL), "error_excerpt": redact_text(type(exc).__name__, extra_secrets=extra_secrets),
        }


def run_disposable_file_fixture(root: Path, fixture_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(root / "nova"))
    from assistant.action_broker import ActionBroker
    from assistant.action_context import ActionContext, arguments_hash

    target = fixture_root / "file-actions" / "fixture.txt"
    args = {"path": str(target), "content": "fixture"}
    context = ActionContext(
        tool="write_file", arguments_sha256=arguments_hash("write_file", args),
        owner_id="manual-harness", scope="disposable-fixture", session_id="fixture-session",
        task_id="fixture-task", target="fixture.txt", observations={"fixture": True},
    )
    broker = ActionBroker(
        {"security": {"profile": "balanced", "approval_timeout_seconds": 5}},
        tool_names={"write_file"},
    )
    broker.set_approval_handler(lambda row: broker.approve(row["request_id"], mode="once"))
    calls = []

    def write_once():
        calls.append(1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture", encoding="utf-8")
        return {"ok": True}

    result = broker.execute("write_file", args, context, write_once)
    pending = len(broker.pending())
    clean = bool(result.get("ok")) and calls == [1] and target.is_file() and pending == 0
    return {"status": "PASS" if clean else "FAIL", "detail": "disposable_fixture_only", "pending_requests": pending}


def prompt_manual(non_interactive: bool) -> list[dict[str, Any]]:
    results = []
    for check in MANUAL_CHECKS:
        print(f"\n[{check['id']}] {check['title']}\nPrecondición: {check['precondition']}\nPasos: {check['steps']}\nEsperado: {check['expected']}")
        if non_interactive:
            confirmed, status = False, "NOT_RUN"
        else:
            confirmed = input("¿Observaste la precondición exacta? [SI/NO]: ").strip().casefold() in {"si", "sí"}
            answer = input("Resultado [PASS/FAIL/NOT_RUN]: ").strip().upper()
            status = answer if answer in {"PASS", "FAIL", "NOT_RUN"} else "FAIL"
            if status == "PASS" and not confirmed:
                status = "FAIL"
        results.append({
            "id": check["id"], "description": check["title"], "required": True,
            "precondition_confirmed": confirmed, "status": status,
        })
    return results


def evidence_exit_code(results: list[dict[str, Any]]) -> int:
    if any(row.get("status") == "FAIL" for row in results):
        return 1
    if any(row.get("required") and row.get("status") != "PASS" for row in results):
        return 2
    return 0


def write_evidence(output_dir: Path, evidence: dict[str, Any], *, extra_secrets: Iterable[str] = ()) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_value(evidence, extra_secrets=extra_secrets)
    json_path = output_dir / "nova-v010-validation.json"
    tmp = output_dir / ".nova-v010-validation.tmp"
    tmp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, json_path)
    summary_path = output_dir / "summary.txt"
    summary_path.write_text(
        "Nova v0.10.0 Windows validation\n"
        + "\n".join(f"{row['id']}: {row['status']}" for row in safe["checks"])
        + "\n", encoding="utf-8",
    )
    zip_path = output_dir / "nova-v010-validation-evidence.zip"
    zip_tmp = output_dir / ".nova-v010-validation-evidence.tmp"
    with zipfile.ZipFile(zip_tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(json_path, arcname=json_path.name)
        archive.write(summary_path, arcname=summary_path.name)
    os.replace(zip_tmp, zip_path)
    return json_path, zip_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Validación controlada de Nova v0.10.0 en Windows 11")
    parser.add_argument("--expected-head", default=os.environ.get("NOVA_EXPECTED_HEAD", ""), help="HEAD aprobado por revisión")
    parser.add_argument("--output-dir", default="", help="Directorio para evidencia sanitizada")
    parser.add_argument("--non-interactive", action="store_true", help="Registra UI como NOT_RUN y devuelve error")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    started = utc_now()
    checks: list[dict[str, Any]] = []
    platform_info = windows_platform_info()
    checks.append({"id": "windows_11", "description": "Windows build >= 22000.", "required": True, "status": "PASS" if is_windows_11(platform_info) else "FAIL"})

    actual_head = current_head(root)
    expected_head = str(args.expected_head or "").strip().casefold()
    head_ok = len(expected_head) == 40 and actual_head.casefold() == expected_head
    checks.append({"id": "expected_head", "description": "Checkout coincide con HEAD revisado.", "required": True, "status": "PASS" if head_ok else "FAIL"})

    with tempfile.TemporaryDirectory(prefix="nova-v010-validation-") as td:
        fixture_root = Path(td)
        install = prepare_runtime_fixture(root, fixture_root)
        before = sanitized_runtime_snapshot(fixture_root)
        focal = run_focal(root)
        checks.append({"id": "focal_suites", "description": "Suites focales de seguridad.", "required": True, **focal})
        file_fixture = run_disposable_file_fixture(root, fixture_root)
        checks.append({"id": "disposable_file_fixture", "description": "Archivo limitado a fixture descartable.", "required": True, **file_fixture})
        fixture_process = None
        if is_windows_11(platform_info) and not args.non_interactive:
            try:
                fixture_process = launch_runtime_fixture(install, fixture_root)
                time.sleep(2.0)
                launch_ok = fixture_process.poll() is None
            except Exception:
                launch_ok = False
            checks.append({"id": "fixture_runtime_launch", "description": "Nova se inició desde la copia descartable aislada.", "required": True, "status": "PASS" if launch_ok else "FAIL"})
        checks.extend(prompt_manual(bool(args.non_interactive)))
        after = sanitized_runtime_snapshot(fixture_root)
        orphan_status, orphan_detail = orphan_check(before, after, pending_requests=int(file_fixture.get("pending_requests") or 0))
        checks.append({"id": "automatic_orphan_check", "description": "Sin requests, threads o procesos de fixture huérfanos.", "required": True, "status": orphan_status, "detail": orphan_detail})
        if fixture_process is not None and fixture_process.poll() is None:
            try:
                fixture_process.terminate()
                fixture_process.wait(timeout=5)
            except Exception:
                pass

    evidence = {
        "schema": 2, "product": "Nova", "version": "0.10.0",
        "started_at": started, "finished_at": utc_now(),
        "expected_head": expected_head, "actual_head": actual_head,
        "platform": {**platform_info, "python": platform.python_version()},
        "runtime_before": before, "runtime_after": after, "checks": checks,
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = Path(args.output_dir).resolve() if args.output_dir else root / "validation-evidence" / f"v010-{stamp}"
    json_path, zip_path = write_evidence(output, evidence)
    code = evidence_exit_code(checks)
    print(f"Evidencia JSON: {json_path}")
    print(f"Evidencia ZIP: {zip_path}")
    print("PASS" if code == 0 else "FAIL: existen comprobaciones fallidas u obligatorias no ejecutadas")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
