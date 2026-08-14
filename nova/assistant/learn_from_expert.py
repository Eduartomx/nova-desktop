from __future__ import annotations

"""Learn-from-Expert compatibility façade with deterministic SQLite cleanup.

The learning implementation remains unchanged in ``learn_from_expert_legacy``.
This façade only hardens the connection context so existing transactions close
the SQLite handle deterministically on Windows while preserving the public
``get_skill_registry`` injection seam used by tests/integrations.
"""

try:
    from . import learn_from_expert_legacy as _legacy
except ImportError:
    import learn_from_expert_legacy as _legacy

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


_original_save_skill = _legacy.ExpertLearning.save_skill


def _save_skill_with_public_registry(self, *args, **kwargs):
    # Methods in the preserved implementation keep their original module
    # globals. Synchronize the public seam immediately before the call so a
    # caller patching assistant.learn_from_expert.get_skill_registry retains the
    # exact pre-facade behavior.
    _legacy.get_skill_registry = globals().get("get_skill_registry", _legacy.get_skill_registry)
    return _original_save_skill(self, *args, **kwargs)


_legacy.ExpertLearning._connect = _connect_and_close
_legacy.ExpertLearning.save_skill = _save_skill_with_public_registry
ExpertLearning = _legacy.ExpertLearning
