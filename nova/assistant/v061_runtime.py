from __future__ import annotations

_INSTALLED = False


def install_v061():
    global _INSTALLED
    if _INSTALLED:
        return
    from .v061_tools import install_tools_v061
    install_tools_v061()
    _INSTALLED = True
