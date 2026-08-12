from __future__ import annotations

_INSTALLED = False


def install_v066():
    global _INSTALLED
    if _INSTALLED:
        return

    # El orden importa: primero capacidades, después UI y finalmente wrappers
    # de profiling para medir el comportamiento final de cada capa.
    from .v066_tools import install_tools_v066
    from .v066_doctor import install_doctor_v066
    from .v066_agent import install_agent_v066
    from .v066_ui import install_ui_v066
    from .v066_profiler import install_profiler_v066

    install_tools_v066()
    install_doctor_v066()
    install_agent_v066()
    install_ui_v066()
    install_profiler_v066()
    _INSTALLED = True
