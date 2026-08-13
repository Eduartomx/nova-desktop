from __future__ import annotations

"""Corrige la presentación del estado y activa la capa de confiabilidad Gaming."""


def _install_reliability_extensions():
    from .gaming_reliability import install_gaming_reliability
    install_gaming_reliability()

    # core_runtime importa install_ui_gaming más tarde. Envolver el instalador
    # aquí permite añadir el puente de eventos sin introducir otra cadena de
    # bootstrap versionada ni reescribir la UI base.
    from . import ui_gaming
    if not getattr(ui_gaming, "_nova_gaming_events_installer_patched", False):
        original_install_ui_gaming = ui_gaming.install_ui_gaming

        def install_ui_gaming_with_events():
            result = original_install_ui_gaming()
            from .ui_gaming_events import install_ui_gaming_events
            install_ui_gaming_events()
            return result

        ui_gaming.install_ui_gaming = install_ui_gaming_with_events
        ui_gaming._nova_gaming_events_installer_patched = True


def install_gaming_status_sync():
    from .gaming_awareness import GamingAwarenessManager

    Manager = GamingAwarenessManager
    if getattr(Manager, "_nova_gaming_status_sync_patched", False):
        _install_reliability_extensions()
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
    _install_reliability_extensions()
    return Manager
