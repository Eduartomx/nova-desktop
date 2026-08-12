from __future__ import annotations

from .profiler import get_profiler
from .self_repair import SelfRepairManager


def install_doctor_v066():
    from . import doctor as mod

    Doctor = mod.NovaDoctor
    if getattr(Doctor, "_nova_v066_patched", False):
        return mod

    original_run = Doctor.run
    original_format = Doctor.format_text

    def run(self):
        report = original_run(self)
        try:
            manager = SelfRepairManager(self.config, self.memory)
            report["repairs"] = manager.available_actions(report)
        except Exception:
            report["repairs"] = []
        try:
            report["performance"] = get_profiler(self.config).summary(hours=24)
        except Exception:
            report["performance"] = {"ok": False, "operations": []}
        return report

    @staticmethod
    def format_text(report):
        text = original_format(report)
        repairs = list(report.get("repairs") or [])
        if repairs:
            text += "\n\nReparaciones disponibles:"
            for item in repairs[:8]:
                text += f"\n- {item.get('title')}: {item.get('detail')}"
            text += "\nAbre Nova Doctor para ejecutar una reparación con confirmación."
        perf = report.get("performance") if isinstance(report.get("performance"), dict) else {}
        slow = list(perf.get("slow_operations") or [])
        if slow:
            text += "\n\nRendimiento: " + ", ".join(
                f"{x.get('operation')} {x.get('avg_ms')} ms" for x in slow[:4]
            )
        return text

    Doctor.run = run
    Doctor.format_text = format_text
    Doctor._nova_v066_patched = True
    return mod
