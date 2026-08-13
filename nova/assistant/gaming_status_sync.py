from __future__ import annotations

"""Sincronización de estado visual para Gaming Awareness.

0.9.5 conservaba correctamente el historial interno de la última liberación de
Qwen para poder restaurarlo al salir del juego, pero ese historial se exponía
como si siguiera siendo el estado actual cuando Gaming Mode ya estaba inactivo.
Este adaptador separa estado actual de historial sin alterar la lógica de
restauración del manager.
"""


def install_gaming_status_sync():
    from .gaming_awareness import GamingAwarenessManager

    Manager = GamingAwarenessManager
    if getattr(Manager, "_nova_gaming_status_sync_patched", False):
        return Manager

    original_status = Manager.status

    def status(self, refresh: bool = False):
        report = dict(original_status(self, refresh=refresh))
        # Estos campos pertenecen a la sesión de juego que acaba de terminar.
        # Se conservan con nombre explícito para diagnóstico/historial, pero no
        # deben hacer parecer que Qwen sigue liberado cuando el modo ya es normal.
        report["last_release_reason"] = report.get("release_reason") or ""
        report["last_release_vram_reclaimed_mb"] = report.get("vram_reclaimed_mb") or 0.0
        if not report.get("active"):
            report["release_reason"] = ""
            report["llm_released"] = False
            report["vram_reclaimed_mb"] = 0.0
        return report

    Manager.status = status
    Manager._nova_gaming_status_sync_patched = True
    return Manager
