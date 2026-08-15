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


def _read_recovery(path: Path) -> tuple[dict[str, Any], bool]:
    path = Path(path)
    if not path.exists():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}, True
        return data, False
    except Exception:
        # Startup itself fails closed on a corrupt journal; Doctor may still be
        # called directly by tests/repair tools, so expose the condition safely.
        return {}, True


def updater_diagnostic(root: Path, *, supervisor_probe=None) -> dict[str, Any]:
    root = Path(root)
    probe = supervisor_probe or supervisor_status
    mutex = probe()
    active = mutex.get("active")
    last = _read_json(root / "data" / "update_last.json")
    recovery, recovery_corrupt = _read_recovery(root / "data" / "update_recovery.json")

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

    recovery_state = str(recovery.get("state") or recovery.get("status") or "")
    recovery_required = bool(recovery.get("recovery_required")) or recovery_corrupt
    if recovery_corrupt:
        parts.append("journal de recuperación corrupto/no verificable")
    elif recovery_required:
        generation = int(recovery.get("generation") or 0)
        parts.append("recuperación pendiente" + (f" ({recovery_state}, gen {generation})" if recovery_state else ""))
    else:
        parts.append("sin recuperación pendiente")

    remaining: list[int] = []
    if recovery_state in {"pip_termination_unconfirmed", "waiting_for_processes"}:
        rows = recovery.get("remaining_processes") or []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    pid = int(row.get("pid") or 0)
                except Exception:
                    pid = 0
                if pid > 0:
                    remaining.append(pid)
        # Compatibility with the pre-schema journal: PID-only values are shown
        # for diagnosis but are never used as proof of identity or termination.
        if not remaining:
            for raw in (recovery.get("remaining_pids") or []):
                try:
                    pid = int(raw)
                except Exception:
                    continue
                if pid > 0:
                    remaining.append(pid)
        remaining = sorted(set(remaining))[:32]

    pip_unconfirmed = recovery_state == "pip_termination_unconfirmed"
    if pip_unconfirmed:
        parts.append("terminación de pip no confirmada")
    if remaining:
        parts.append("PID restantes: " + ", ".join(str(pid) for pid in remaining))

    probe_error = str(mutex.get("error") or "")
    if probe_error:
        parts.append("mutex no verificable")

    severity = "error" if recovery_corrupt else ("warn" if recovery_required or active is None else "ok")
    return {
        "name": "Updater residente",
        "status": severity,
        "detail": " · ".join(parts),
        "updater": {
            "supervisor_active": active,
            "last_state": last_state,
            "last_ok": last.get("ok") if last else None,
            "recovery_required": recovery_required,
            "recovery_state": recovery_state or ("corrupt" if recovery_corrupt else ""),
            "recovery_generation": int(recovery.get("generation") or 0) if recovery else 0,
            "journal_corrupt": recovery_corrupt,
            "pip_termination_unconfirmed": pip_unconfirmed,
            "remaining_pids": remaining,
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
