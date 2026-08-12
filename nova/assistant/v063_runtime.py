from __future__ import annotations

_INSTALLED = False


def install_v063():
    global _INSTALLED
    if _INSTALLED:
        return
    from .v063_memory import install_memory_v063
    from .v063_agent import install_agent_v063
    from .v063_tools import install_tools_v063
    from .v063_doctor import install_doctor_v063
    from .v063_ui import install_ui_v063

    install_memory_v063()
    install_agent_v063()
    install_tools_v063()
    install_doctor_v063()
    install_ui_v063()
    _INSTALLED = True
