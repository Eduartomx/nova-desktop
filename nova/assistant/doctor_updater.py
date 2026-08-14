from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .update_supervisor import supervisor_status


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def updater_diagnostic(root: Path, *, supervisor_probe=None) -> dict[str, Any]:
    root = Path(root)
    probe = supervisor_probe or supervisor_status
    mutex = probe()
    active = mutex.get("active")
    last = _read_json(root / "data" / "update_last.json")
    recovery = _read_json(root / "data" / "update_recovery.json")

    active_text = "activo" if active is True else ("inactivo" if active is False else "no verificable")
    parts = [f"supervisor {active_text}"]
    last_state = str(last.get("state") or "")
    if last:
        before = str(last.get("before") or "?")
        after = str(last.get("after") or "?")
        if last.get("ok"):
            parts.append(f"última actualización correcta {before} → {after}")
        else:
            parts.append("última actualización con atención" + (f" ({last_state})" if last_state else ""))
    else:
        parts.append("sin resultado de actualización registrado")

    recovery_required = bool(recovery.get("recovery_required"))
    recovery_state = str(recovery.get("status") or "")
    if recovery_required:
        parts.append("recuperación pendiente")
    else:
        parts.append("sin recuperación pendiente")

    remaining = [
        int(pid) for pid in (recovery.get("remaining_pids") or [])
        if str(pid).isdigit() and int(pid) > 0
    ][:32]
    if recovery_state == "pip_termination_unconfirmed":
        parts.append("terminación de pip no confirmada")
        if remaining:
            parts.append("PID restantes: " + ", ".join(str(pid) for pid in remaining))

    probe_error = str(mutex.get("error") or "")
    if probe_error:
        parts.append("mutex no verificable")

    severity = "warn" if recovery_required or active is None else "ok"
    return {
        "name": "Updater residente",
        "status": severity,
        "detail": " · ".join(parts),
        "updater": {
            "supervisor_active": active,
            "last_state": last_state,
            "last_ok": last.get("ok") if last else None,
            "recovery_required": recovery_required,
            "recovery_state": recovery_state,
            "pip_termination_unconfirmed": recovery_state == "pip_termination_unconfirmed",
            "remaining_pids": remaining if recovery_state == "pip_termination_unconfirmed" else [],
        },
    }


def install_doctor_updater():
    from .doctor import NovaDoctor

    Doctor = NovaDoctor
    if getattr(Doctor, "_nova_updater_diagnostics_patched", False):
        return Doctor
    original_run = Doctor.run

    def run(self):
        report = original_run(self)
        check = updater_diagnostic(self.root)
        checks = list(report.get("checks") or [])
        checks.append(check)
        report["checks"] = checks
        if check.get("status") == "error":
            report["severity"] = "error"
            report["ok"] = False
        elif check.get("status") == "warn" and report.get("severity") == "ok":
            report["severity"] = "warn"
        return report

    Doctor.run = run
    Doctor._nova_updater_diagnostics_patched = True
    return Doctor
