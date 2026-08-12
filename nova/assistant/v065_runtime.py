from __future__ import annotations

_INSTALLED = False


def install_v065():
    global _INSTALLED
    if _INSTALLED:
        return

    from .v065_memory import install_memory_v065
    from .v065_tools import install_tools_v065
    from .v065_agent import install_agent_v065
    from .v065_ui import install_ui_v065

    install_memory_v065()
    install_tools_v065()
    install_agent_v065()
    install_ui_v065()
    _INSTALLED = True
