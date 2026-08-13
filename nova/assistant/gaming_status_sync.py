from __future__ import annotations

"""Corrige la presentación del estado de Gaming Awareness tras salir de un juego."""


def install_gaming_status_sync():
    from .gaming_awareness import GamingAwarenessManager

    Manager = GamingAwarenessManager
    if getattr(Manager, "_nova_gaming_status_sync_patched", False):
        return Manager

    original_status = Manager.status
    original_format_status = Manager.format_status

    def status(self, refresh: bool = False):
        report = dict(original_status(self, refresh=refresh))
        report["last_release_reason"] = report.get("release_reason") or ""
        report["last_release_vram_reclaimed_mb"] = report.get("vram_reclaimed_mb") or 0.0
        if not report.get("active"):
            report["release_reason"] = ""
            report["llm_released"] = False
            report["vram_reclaimed_mb"] = 0.0
        return report

    def format_status(report):
        text = original_format_status(report)
        if not report.get("active") and report.get("last_release_reason"):
            reclaimed = float(report.get("last_release_vram_reclaimed_mb") or 0)
            suffix = f" · ~{reclaimed:.0f} MB recuperados" if reclaimed else ""
            text += f"\n- Última liberación: {report.get('last_release_reason')}{suffix}"
        return text

    Manager.status = status
    Manager.format_status = staticmethod(format_status)
    Manager._nova_gaming_status_sync_patched = True
    return Manager
