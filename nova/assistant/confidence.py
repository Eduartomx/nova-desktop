from __future__ import annotations

"""Confidence Engine compatibility façade with deterministic SQLite cleanup.

The confidence implementation remains unchanged in ``confidence_legacy``. This
façade only hardens the connection context so every existing transaction closes
its SQLite handle deterministically on Windows.
"""

try:
    from . import confidence_legacy as _legacy
except ImportError:
    import confidence_legacy as _legacy

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


_legacy.ConfidenceEngine._connect = _connect_and_close
ConfidenceEngine = _legacy.ConfidenceEngine
