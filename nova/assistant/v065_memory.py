from __future__ import annotations

from .continuity import ContinuityEngine


_TASK_CHECKPOINT_STATES = {
    "completed", "complete", "done", "success", "succeeded",
    "failed", "cancelled", "canceled", "paused", "blocked",
}
_EVENT_CHECKPOINT_TYPES = {
    "completed", "complete", "failed", "error", "blocked", "paused",
    "cancelled", "canceled", "replan", "replanned",
}


def install_memory_v065():
    from . import memory as memory_mod

    MemoryStore = memory_mod.MemoryStore
    if getattr(MemoryStore, "_nova_v065_patched", False):
        return MemoryStore

    original_init = MemoryStore.__init__
    original_create_task = MemoryStore.create_task
    original_update_task = MemoryStore.update_task
    original_upsert_task_step = MemoryStore.upsert_task_step
    original_add_task_event = MemoryStore.add_task_event
    original_stats = getattr(MemoryStore, "stats", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.continuity = ContinuityEngine(self, config={"enabled": False})

    def configure_continuity(self, config=None):
        engine = getattr(self, "continuity", None)
        if engine is None:
            engine = ContinuityEngine(self, config=config or {})
            self.continuity = engine
        else:
            engine.configure(config or {})
            engine.ensure_schema()
        return engine

    def continuity_resume(self, workspace_id=None, any_if_none=False):
        return self.continuity.resume(workspace_id=workspace_id, any_if_none=bool(any_if_none))

    def continuity_pending(self, workspace_id=None, any_if_none=False):
        return self.continuity.pending(workspace_id=workspace_id, any_if_none=bool(any_if_none))

    def continuity_history(self, workspace_id=None, limit=None, any_if_none=False):
        return self.continuity.history(workspace_id=workspace_id, limit=limit, any_if_none=bool(any_if_none))

    def continuity_checkpoint(self, **kwargs):
        return self.continuity.checkpoint(**kwargs)

    def continuity_close(self, workspace_id=None, status="completed", summary=""):
        return self.continuity.close(workspace_id=workspace_id, status=status, summary=summary)

    def _auto_enabled(self):
        engine = getattr(self, "continuity", None)
        return bool(engine and engine.enabled and engine.config.get("auto_checkpoint_tasks", True))

    def _safe_task_checkpoint(self, task_id: int, reason: str):
        if not _auto_enabled(self):
            return
        try:
            self.continuity.checkpoint_from_task(int(task_id), reason=reason)
        except Exception:
            # Continuity nunca debe romper el Task Engine.
            pass

    def create_task(self, goal, plan, status="planned"):
        task_id = original_create_task(self, goal, plan, status)
        if _auto_enabled(self):
            try:
                task = self.get_task(int(task_id))
                workspace_id = task.get("workspace_id") if task else None
                self.continuity.start(
                    workspace_id=workspace_id,
                    goal=str(goal or ""),
                    task_id=int(task_id),
                    title=str(goal or "")[:160],
                )
                self.continuity.checkpoint(
                    workspace_id=workspace_id,
                    task_id=int(task_id),
                    goal=str(goal or ""),
                    summary=f"Tarea #{task_id} creada: {goal}",
                    pending=[str(x.get("description") or "").strip() for x in (plan or {}).get("steps", []) if str(x.get("description") or "").strip()],
                    metadata={"reason": "task_created", "task_status": status},
                    kind="task",
                    session_status="active",
                )
            except Exception:
                pass
        return task_id

    def update_task(self, task_id, status=None, summary=None):
        result = original_update_task(self, task_id, status=status, summary=summary)
        state = str(status or "").casefold()
        if summary is not None or state in _TASK_CHECKPOINT_STATES:
            _safe_task_checkpoint(self, int(task_id), "task_update")
        return result

    def upsert_task_step(self, task_id, step_index, description, success_criteria, status="pending",
                         attempts=0, result="", verifier=""):
        value = original_upsert_task_step(
            self, task_id, step_index, description, success_criteria,
            status=status, attempts=attempts, result=result, verifier=verifier,
        )
        if str(status or "").casefold() in _TASK_CHECKPOINT_STATES:
            _safe_task_checkpoint(self, int(task_id), "task_step")
        return value

    def add_task_event(self, task_id, event_type, message, data=None):
        value = original_add_task_event(self, task_id, event_type, message, data)
        if str(event_type or "").casefold() in _EVENT_CHECKPOINT_TYPES:
            _safe_task_checkpoint(self, int(task_id), f"task_event:{event_type}")
        return value

    def stats(self):
        base = dict(original_stats(self)) if callable(original_stats) else {}
        try:
            base.update(self.continuity.stats())
        except Exception:
            base.setdefault("continuity_sessions", 0)
            base.setdefault("continuity_checkpoints", 0)
            base.setdefault("continuity_active", 0)
        return base

    MemoryStore.__init__ = init
    MemoryStore.configure_continuity = configure_continuity
    MemoryStore.continuity_resume = continuity_resume
    MemoryStore.continuity_pending = continuity_pending
    MemoryStore.continuity_history = continuity_history
    MemoryStore.continuity_checkpoint = continuity_checkpoint
    MemoryStore.continuity_close = continuity_close
    MemoryStore.create_task = create_task
    MemoryStore.update_task = update_task
    MemoryStore.upsert_task_step = upsert_task_step
    MemoryStore.add_task_event = add_task_event
    MemoryStore.stats = stats
    MemoryStore._nova_v065_patched = True
    return MemoryStore
