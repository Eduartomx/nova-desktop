from __future__ import annotations

"""Install the Action Broker at the final LocalTools dispatch boundary."""

import os
from pathlib import Path
import uuid
from typing import Any

from .action_broker import ActionBroker
from .action_context import ActionContext, build_action_context


def install_tools_action_guard():
    from . import tools as mod

    LocalTools = mod.LocalTools
    if getattr(LocalTools, "_nova_action_broker", False):
        return mod

    original_init = LocalTools.__init__
    original_execute = LocalTools.execute_tool

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.action_owner_id = uuid.uuid4().hex
        self.action_scope = "local-ui"
        self.action_session_id = str(os.getpid())
        self.action_task_id = ""
        self.action_user_text = ""
        names = {
            str(schema.get("function", {}).get("name") or "")
            for schema in mod.TOOL_SCHEMAS
            if str(schema.get("function", {}).get("name") or "")
        }
        data_dir = Path(__file__).resolve().parent.parent / "data"
        self.action_broker = ActionBroker(
            self.config,
            tool_names=names,
            audit_path=data_dir / "action_audit.jsonl",
        )

    def _context(self, name: str, arguments: dict[str, Any], supplied: ActionContext | None = None):
        if isinstance(supplied, ActionContext):
            return supplied
        return build_action_context(
            name,
            arguments,
            tools=self,
            task_id=getattr(self, "action_task_id", ""),
            user_text=getattr(self, "action_user_text", ""),
        )

    def execute_tool(self, name: str, arguments: dict[str, Any] | None = None, action_context: ActionContext | None = None):
        tool_name = str(name or "")
        args = dict(arguments or {})
        initial = _context(self, tool_name, args, action_context)

        def invoke():
            self._action_broker_executing = True
            try:
                return original_execute(self, tool_name, args)
            finally:
                self._action_broker_executing = False

        return self.action_broker.execute(
            tool_name,
            args,
            initial,
            invoke,
            context_provider=lambda: _context(self, tool_name, args, None),
        )

    LocalTools.__init__ = init
    LocalTools.execute_tool = execute_tool
    LocalTools.execute = execute_tool
    LocalTools._nova_action_broker = True
    return mod
