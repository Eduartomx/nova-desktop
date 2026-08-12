from __future__ import annotations

"""Capa de compatibilidad v0.6 sobre instalaciones históricas v0.5."""

_INSTALLED = False


def install_v060():
    global _INSTALLED
    if _INSTALLED:
        return
    from .v060_memory import install_memory_v060
    from .v060_tools import install_tools_v060
    from .v060_agent import install_agent_v060
    from .v060_ui import install_ui_v060
    install_memory_v060()
    install_tools_v060()
    install_agent_v060()
    install_ui_v060()
    _INSTALLED = True
