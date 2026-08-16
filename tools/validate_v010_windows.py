from __future__ import annotations

"""Safe Windows 11 validation harness for Nova v0.10.0.

This runner executes only mocked/local focal tests and prints the controlled UI
checklist. It never purchases, sends, publishes, deletes or executes a
destructive command.
"""

import platform
from pathlib import Path
import subprocess
import sys


FOCAL = [
    "tests.test_action_broker", "tests.test_action_policy", "tests.test_action_task_engine",
    "tests.test_ui_action_approval", "tests.test_action_tool_guards",
    "tests.test_repository_intelligence", "tests.test_repository_routing",
]

CHECKLIST = [
    "Permitir una acción la ejecuta exactamente una vez.",
    "Denegar no produce efectos.",
    "El grant de tarea no funciona fuera de la tarea.",
    "Alto riesgo vuelve a solicitar permiso.",
    "Cambiar archivo, ventana, control, URL o formulario invalida el permiso.",
    "Cancelar y Detener automatización liberan la espera.",
    "Nova oculta notifica y permite abrir la aprobación.",
    "Shutdown y update no dejan workers pendientes.",
    "Qué cambió responde con changelog/release y evidencia.",
    "Offline responde con datos locales/cache sin inventar.",
    "No quedan acciones, threads ni procesos huérfanos.",
]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    print(f"Sistema: {platform.platform()}")
    if platform.system() != "Windows":
        print("DETENIDO: este harness manual debe ejecutarse en Windows 11.")
        return 2
    command = [sys.executable, "-m", "unittest", "-v", *FOCAL]
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(root / "nova")
    result = subprocess.run(command, cwd=str(root), env=env, check=False)
    print("\nChecklist manual controlado (registrar PASS/FAIL sin datos privados):")
    for index, item in enumerate(CHECKLIST, 1):
        print(f"{index:02d}. [ ] {item}")
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
