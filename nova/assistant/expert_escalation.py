from __future__ import annotations

"""Expert Escalation compatibility façade with deterministic SQLite cleanup.

The escalation implementation remains unchanged in ``expert_escalation_legacy``.
Only its connection context is hardened so every existing transaction closes the
SQLite handle deterministically on Windows. Public module assignments are
mirrored into the preserved implementation so resilience patches keep their
pre-facade module-global semantics.
"""

import sys
import types

try:
    from . import expert_escalation_legacy as _legacy
except ImportError:
    import expert_escalation_legacy as _legacy

for _name in dir(_legacy):
    if _name.startswith("__"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_legacy, _name)


class _ClosingSQLiteContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.__enter__()
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        try:
            return self.connection.__exit__(exc_type, exc, tb)
        finally:
            self.connection.close()


def _connect_and_close(self):
    conn = _legacy.sqlite3.connect(self.db_path, timeout=5.0)
    conn.row_factory = _legacy.sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return _ClosingSQLiteContext(conn)


class _ExpertEscalationProxyModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if not name.startswith("__") and hasattr(_legacy, name):
            setattr(_legacy, name, value)


_legacy.ExpertEscalation._connect = _connect_and_close
ExpertEscalation = _legacy.ExpertEscalation
sys.modules[__name__].__class__ = _ExpertEscalationProxyModule
