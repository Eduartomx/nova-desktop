from __future__ import annotations


def install_doctor_v063():
    from . import doctor as mod

    Doctor = mod.NovaDoctor
    if getattr(Doctor, "_nova_v063_patched", False):
        return mod

    original_run = Doctor.run

    def run(self):
        report = original_run(self)
        if self.memory is None or not hasattr(self.memory, "semantic_status"):
            return report
        try:
            active = self.memory.active_workspace()
            wid = int(active["id"]) if active else None
            status = self.memory.semantic_status(workspace_id=wid, refresh=True)
            if not status.get("enabled"):
                item = self._result("Semantic Memory", "warn", "Desactivada en config")
            elif not status.get("model_available"):
                item = self._result(
                    "Semantic Memory",
                    "warn",
                    f"{status.get('detail')} · instala con: {status.get('install_command')}",
                    semantic=status,
                )
            else:
                item = self._result(
                    "Semantic Memory",
                    "ok",
                    f"{status.get('model')} · {status.get('indexed', 0)}/{status.get('total_candidates', 0)} recuerdos indexados",
                    semantic=status,
                )
            report.setdefault("checks", []).append(item)
            errors = any(x.get("status") == "error" for x in report["checks"])
            warns = any(x.get("status") == "warn" for x in report["checks"])
            report["severity"] = "error" if errors else ("warn" if warns else "ok")
            report["ok"] = not errors
        except Exception as exc:
            report.setdefault("checks", []).append(self._result("Semantic Memory", "warn", str(exc)))
            if report.get("severity") == "ok":
                report["severity"] = "warn"
        return report

    Doctor.run = run
    Doctor._nova_v063_patched = True
    return mod
