from __future__ import annotations

"""Skills compatibility façade with deterministic SQLite handle cleanup.

The declarative Skills Engine implementation remains unchanged in
``skills_legacy``. This façade only hardens the connection context used by all
existing methods: transaction semantics are preserved and the underlying
SQLite handle is always closed when the ``with`` block exits. This matters on
Windows, where an open database handle prevents updater/test directories from
being replaced or removed.
"""

try:
    from . import skills_legacy as _legacy
except ImportError:
    import skills_legacy as _legacy

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
    conn.execute("PRAGMA foreign_keys=ON")
    return _ClosingSQLiteContext(conn)


# Existing SkillRegistry methods resolve ``self._connect`` dynamically, so one
# targeted method replacement hardens every read/write transaction without
# changing their SQL, return values, or public class identity.
_legacy.SkillRegistry._connect = _connect_and_close
SkillRegistry = _legacy.SkillRegistry
