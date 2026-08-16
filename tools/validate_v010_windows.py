from __future__ import annotations

"""Verifiable, non-destructive Windows 11 harness for Nova v0.10.0."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import zipfile


FOCAL = [
    "tests.test_action_broker", "tests.test_action_policy", "tests.test_action_task_engine",
    "tests.test_ui_action_approval", "tests.test_action_tool_guards",
    "tests.test_repository_intelligence", "tests.test_repository_routing",
    "tests.test_v010_windows_harness",
]

MANUAL_CHECKS = [
    ("allow_once", "Permitir una vez ejecuta exactamente una vez."),
    ("deny", "Denegar no produce efectos."),
    ("close_denies", "Cerrar la tarjeta equivale a denegar."),
    ("timeout", "El timeout impide la ejecución."),
    ("grant_scope", "El grant no cruza sesión ni tarea."),
    ("high_risk_reprompts", "Alto riesgo vuelve a pedir autorización."),
    ("neutral_submit", "Un submit con selector neutro no evade la autorización."),
    ("toctou", "Cambiar archivo, ventana, control, URL o formulario invalida el permiso."),
    ("stop_shutdown", "Detener y shutdown liberan la espera."),
    ("same_task_resume", "La tarea aprobada continúa exactamente el mismo task_id y paso."),
    ("hidden_notification", "Nova oculta notifica y permite abrir la aprobación."),
    ("repository_online_offline", "Versión y changelog funcionan online y offline con evidencia."),
    ("no_orphans", "No quedan acciones, ventanas, threads ni procesos huérfanos."),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitized_runtime_snapshot() -> dict:
    result = {"thread_count": threading.active_count(), "related_process_count": 0, "child_process_count": 0}
    try:
        import psutil
        current = psutil.Process()
        result["child_process_count"] = len(current.children(recursive=True))
        related = 0
        for process in psutil.process_iter(["name"]):
            try:
                name = str(process.info.get("name") or "").casefold()
                if "nova" in name or name in {"python.exe", "pythonw.exe"}:
                    related += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        result["related_process_count"] = related
    except Exception:
        result["process_observation"] = "unavailable"
    return result


def current_head(root: Path) -> str:
    run = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
        timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return run.stdout.strip() if run.returncode == 0 else ""


def run_focal(root: Path) -> tuple[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "nova")
    run = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *FOCAL], cwd=str(root), env=env,
        capture_output=True, text=True, timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return ("PASS" if run.returncode == 0 else "FAIL"), f"returncode={run.returncode}"


def run_disposable_file_fixture(root: Path) -> tuple[str, str]:
    sys.path.insert(0, str(root / "nova"))
    from assistant.action_broker import ActionBroker
    from assistant.action_context import ActionContext, arguments_hash

    with tempfile.TemporaryDirectory(prefix="nova-v010-validation-") as td:
        target = Path(td) / "fixture.txt"
        args = {"path": str(target), "content": "fixture"}
        context = ActionContext(
            tool="write_file", arguments_sha256=arguments_hash("write_file", args),
            owner_id="manual-harness", scope="disposable-fixture", session_id="fixture-session",
            task_id="fixture-task", target="fixture.txt", explicit_intent=True,
            observations={"fixture": True},
        )
        broker = ActionBroker(
            {"security": {"profile": "balanced", "approval_timeout_seconds": 5}},
            tool_names={"write_file"},
        )
        broker.set_approval_handler(lambda row: broker.approve(row["request_id"], mode="once"))
        calls = []

        def write_once():
            calls.append(1)
            target.write_text("fixture", encoding="utf-8")
            return {"ok": True}

        result = broker.execute("write_file", args, context, write_once)
        clean = bool(result.get("ok")) and calls == [1] and target.is_file() and not broker.pending()
        return ("PASS" if clean else "FAIL"), "disposable_fixture_only"


def prompt_manual(non_interactive: bool) -> list[dict]:
    results = []
    for key, description in MANUAL_CHECKS:
        if non_interactive:
            status = "NOT_RUN"
        else:
            print(f"\n{description}")
            while True:
                answer = input("Resultado [PASS/FAIL/NOT_RUN]: ").strip().upper()
                if answer in {"PASS", "FAIL", "NOT_RUN"}:
                    status = answer
                    break
        results.append({"id": key, "description": description, "required": True, "status": status})
    return results


def evidence_exit_code(results: list[dict]) -> int:
    if any(row.get("status") == "FAIL" for row in results):
        return 1
    if any(row.get("required") and row.get("status") != "PASS" for row in results):
        return 2
    return 0


def write_evidence(output_dir: Path, evidence: dict) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "nova-v010-validation.json"
    tmp = output_dir / ".nova-v010-validation.tmp"
    tmp.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, json_path)
    summary_path = output_dir / "summary.txt"
    summary_path.write_text(
        "Nova v0.10.0 Windows validation\n"
        + "\n".join(f"{row['id']}: {row['status']}" for row in evidence["checks"])
        + "\n",
        encoding="utf-8",
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
    parser.add_argument("--non-interactive", action="store_true", help="Registra comprobaciones UI como NOT_RUN y devuelve error")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    started = utc_now()
    before = sanitized_runtime_snapshot()
    checks = []

    system_ok = platform.system() == "Windows" and platform.release() in {"10", "11"}
    checks.append({"id": "windows_11", "description": "Harness ejecutado en Windows 11.", "required": True, "status": "PASS" if system_ok else "FAIL"})

    actual_head = current_head(root)
    expected_head = str(args.expected_head or "").strip().casefold()
    head_ok = len(expected_head) == 40 and actual_head.casefold() == expected_head
    checks.append({"id": "expected_head", "description": "El checkout coincide con el HEAD revisado.", "required": True, "status": "PASS" if head_ok else "FAIL"})

    focal_status, focal_detail = run_focal(root)
    checks.append({"id": "focal_suites", "description": "Suites focales de seguridad.", "required": True, "status": focal_status, "detail": focal_detail})
    fixture_status, fixture_detail = run_disposable_file_fixture(root)
    checks.append({"id": "disposable_file_fixture", "description": "Acción de archivo limitada a fixture descartable.", "required": True, "status": fixture_status, "detail": fixture_detail})
    checks.extend(prompt_manual(bool(args.non_interactive)))

    after = sanitized_runtime_snapshot()
    automatic_orphan_ok = after.get("child_process_count", 0) <= before.get("child_process_count", 0)
    checks.append({
        "id": "automatic_orphan_check", "description": "El harness no dejó procesos hijo adicionales.",
        "required": True, "status": "PASS" if automatic_orphan_ok else "FAIL",
    })

    evidence = {
        "schema": 1, "product": "Nova", "version": "0.10.0",
        "started_at": started, "finished_at": utc_now(),
        "expected_head": expected_head, "actual_head": actual_head,
        "platform": {"system": platform.system(), "release": platform.release(), "python": platform.python_version()},
        "runtime_before": before, "runtime_after": after,
        "checks": checks,
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
