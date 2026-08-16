from __future__ import annotations

"""Task Engine nativo de Nova.

Conserva Planner → Executor → verificación ligera, replan acotado, pausa,
reanudación, cancelación y límites de tiempo/tool-calls. Toda acción sigue
pasando por LocalAgent/LocalTools y sus políticas de seguridad.
"""

import json
import re
import threading
import time
from typing import Any


class TaskEngine:
    def __init__(self, agent, config: dict[str, Any] | None = None, memory=None):
        self.agent = agent
        self.config = config or getattr(agent, "config", {}) or {}
        self.memory = memory or getattr(agent, "memory", None)
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self.current_task_id: int | None = None

    @property
    def settings(self) -> dict[str, Any]:
        cfg = self.config.get("task_engine", {}) if isinstance(self.config, dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def cancel(self):
        self._cancel.set()
        tools = getattr(self.agent, "tools", None)
        broker = getattr(tools, "action_broker", None)
        if broker is not None:
            broker.cancel_all("task_cancelled")
        if self.current_task_id and self.memory is not None:
            try:
                self.memory.update_task(self.current_task_id, status="cancelled", summary="Cancelada por el usuario.")
                self.memory.add_task_event(self.current_task_id, "cancelled", "Cancelada por el usuario.")
            except Exception:
                pass
        return {"ok": True, "cancelled": True}

    def pause(self):
        self._pause.set()
        if self.current_task_id and self.memory is not None:
            try:
                self.memory.update_task(self.current_task_id, status="paused")
                self.memory.add_task_event(self.current_task_id, "paused", "Tarea pausada.")
            except Exception:
                pass
        return {"ok": True, "paused": True}

    def resume(self):
        self._pause.clear()
        if self.current_task_id and self.memory is not None:
            try:
                self.memory.update_task(self.current_task_id, status="running")
                self.memory.add_task_event(self.current_task_id, "resumed", "Tarea reanudada.")
            except Exception:
                pass
        return {"ok": True, "paused": False}

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
        candidates = [raw]
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except Exception:
                continue
        return None

    def plan(self, goal: str) -> dict[str, Any]:
        goal = str(goal or "").strip()
        if not goal:
            return {"goal": "", "steps": []}
        max_steps = max(1, min(int(self.settings.get("max_plan_steps", 8) or 8), 12))
        prompt = (
            "Actúa solo como Planner. NO ejecutes herramientas. Devuelve únicamente JSON con formato "
            '{"steps":[{"description":"...","success_criteria":"..."}]}. '
            f"Máximo {max_steps} pasos concretos y verificables. Objetivo: {goal}"
        )
        try:
            messages = [
                {"role": "system", "content": "Eres el Planner de Nova. Solo produces un plan JSON; no ejecutas acciones."},
                {"role": "user", "content": prompt},
            ]
            data = self.agent._ollama_chat(messages, tools=None)
            content = str((data.get("message") or {}).get("content") or "")
            parsed = self._extract_json(content) or {}
            steps = self._normalize_steps(parsed.get("steps") or [], start_index=1, limit=max_steps)
            if steps:
                return {"goal": goal, "steps": steps, "planner": "ollama"}
        except Exception:
            pass
        return {
            "goal": goal,
            "steps": [{"index": 1, "description": goal, "success_criteria": "El objetivo queda completado y verificado."}],
            "planner": "safe_fallback",
        }

    @staticmethod
    def _normalize_steps(rows, start_index=1, limit=12):
        steps = []
        for offset, row in enumerate(list(rows or [])[:limit]):
            if isinstance(row, str):
                row = {"description": row}
            if not isinstance(row, dict):
                continue
            description = str(row.get("description") or "").strip()
            if not description:
                continue
            steps.append({
                "index": int(start_index) + len(steps),
                "description": description[:1200],
                "success_criteria": str(row.get("success_criteria") or "Resultado comprobado y consistente con el objetivo.")[:800],
            })
        return steps

    def _replan(self, goal: str, failed_step: dict[str, Any], completed: list[dict[str, Any]], start_index: int):
        max_steps = max(1, min(int(self.settings.get("max_plan_steps", 8) or 8), 12))
        done = "; ".join(str(x.get("description") or "")[:220] for x in completed[-6:]) or "ninguno"
        prompt = (
            "REPLANNER de Nova. No ejecutes herramientas. El plan anterior falló. "
            "Devuelve SOLO JSON {\"steps\":[{\"description\":\"...\",\"success_criteria\":\"...\"}]}. "
            f"Objetivo original: {goal}. Pasos ya completados (NO repetir): {done}. "
            f"Paso fallido: {failed_step.get('description')}. Propón otra ruta verificable, máximo {max_steps} pasos."
        )
        try:
            data = self.agent._ollama_chat([
                {"role": "system", "content": "Eres Replanner. Nunca ejecutas acciones y no repites pasos completados."},
                {"role": "user", "content": prompt},
            ], tools=None)
            parsed = self._extract_json(str((data.get("message") or {}).get("content") or "")) or {}
            return self._normalize_steps(parsed.get("steps") or [], start_index=start_index, limit=max_steps)
        except Exception:
            return []

    def _wait_if_paused(self):
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(max(0.05, float(self.settings.get("pause_poll_ms", 150) or 150) / 1000.0))

    @staticmethod
    def _response_success(text: str) -> bool:
        t = str(text or "").casefold()
        negative = (
            "no pude", "no se pudo", "falló", "fallo", "error:", "confirmation_required",
            "alcancé el límite", "no pude conectar con ollama", "requiere confirmación",
            "authorization_denied", "authorization_expired", "authorization_cancelled",
            "acción fue denegada", "autorización expiró", "autorización fue cancelada",
        )
        return bool(t.strip()) and not any(x in t for x in negative)

    def _tool_count(self) -> int:
        try:
            return len(self.agent.last_tool_trace())
        except Exception:
            return 0

    def run(self, goal: str, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.settings.get("enabled", True):
            return {"ok": False, "error": "task_engine_disabled"}
        self._cancel.clear()
        self._pause.clear()
        started = time.monotonic()
        max_minutes = max(1, min(int(self.settings.get("max_task_minutes", 20) or 20), 240))
        max_tool_calls = max(1, min(int(self.settings.get("max_tool_calls", 40) or 40), 500))
        max_replans = max(0, min(int(self.settings.get("max_replans", 2) or 2), 6))
        auto_replan = bool(self.settings.get("auto_replan", True))
        max_retries = max(0, min(int(self.settings.get("max_step_retries", 1) or 1), 3))
        stop_on_failed = bool(self.settings.get("stop_on_failed_step", True))

        plan = plan or self.plan(goal)
        steps = self._normalize_steps(plan.get("steps") or [], start_index=1, limit=30)
        if not steps:
            return {"ok": False, "error": "empty_plan"}
        plan = {**plan, "steps": steps}

        task_id = None
        if self.memory is not None:
            try:
                task_id = self.memory.create_task(str(goal), plan, status="running")
                self.current_task_id = int(task_id)
                tools = getattr(self.agent, "tools", None)
                if tools is not None and hasattr(tools, "action_task_id"):
                    tools.action_task_id = str(task_id)
                for step in steps:
                    self.memory.upsert_task_step(
                        task_id, int(step["index"]), str(step["description"]), str(step["success_criteria"]), status="pending",
                    )
                self.memory.add_task_event(task_id, "started", "Task Engine inició la ejecución.")
            except Exception:
                task_id = None

        results: list[dict[str, Any]] = []
        completed_defs: list[dict[str, Any]] = []
        replans = 0
        total_tool_calls = 0
        pointer = 0
        limit_reason = ""

        while pointer < len(steps):
            self._wait_if_paused()
            if self._cancel.is_set():
                break
            if time.monotonic() - started > max_minutes * 60:
                limit_reason = "max_task_minutes"
                break
            if total_tool_calls >= max_tool_calls:
                limit_reason = "max_tool_calls"
                break

            step = steps[pointer]
            idx = int(step.get("index") or pointer + 1)
            description = str(step.get("description") or "").strip()
            criteria = str(step.get("success_criteria") or "").strip()
            attempts = 0
            success = False
            response = ""
            waiting_for_approval = False
            authorization_terminal = ""

            while attempts <= max_retries and not self._cancel.is_set():
                attempts += 1
                if task_id and self.memory is not None:
                    try:
                        self.memory.upsert_task_step(task_id, idx, description, criteria, status="running", attempts=attempts)
                    except Exception:
                        pass
                instruction = (
                    "TASK ENGINE — ejecuta SOLO el paso actual. No rehagas pasos anteriores.\n"
                    f"Objetivo global: {goal}\n"
                    f"Paso {idx}: {description}\n"
                    f"Criterio de éxito: {criteria or 'resultado verificable'}\n"
                    "Usa herramientas reales si hacen falta y no afirmes éxito sin evidencia."
                )
                broker = getattr(getattr(self.agent, "tools", None), "action_broker", None)
                wait_before = float(getattr(broker, "total_wait_seconds", 0.0) or 0.0)
                response = str(self.agent.ask(instruction) or "")
                wait_after = float(getattr(broker, "total_wait_seconds", 0.0) or 0.0)
                # Human decision time is not autonomous execution time.
                started += max(0.0, wait_after - wait_before)
                total_tool_calls += self._tool_count()
                try:
                    auth_states = {
                        str(row.get("authorization_state") or "")
                        for row in self.agent.last_tool_trace()
                    }
                except Exception:
                    auth_states = set()
                waiting_for_approval = "pending" in auth_states or "waiting_for_approval" in response.casefold()
                if waiting_for_approval:
                    success = False
                    break
                authorization_terminal = next((state for state in ("denied", "expired", "cancelled") if state in auth_states), "")
                if authorization_terminal:
                    success = False
                    break
                success = self._response_success(response)
                if success or total_tool_calls >= max_tool_calls:
                    break

            action_state = "waiting_for_approval" if waiting_for_approval else (authorization_terminal or None)
            results.append({"index": idx, "description": description, "success": success, "attempts": attempts, "result": response, "authorization_state": action_state})
            if task_id and self.memory is not None:
                try:
                    self.memory.upsert_task_step(
                        task_id, idx, description, criteria,
                        status="completed" if success else ("waiting_for_approval" if waiting_for_approval else (authorization_terminal or "failed")), attempts=attempts,
                        result=response[:5000], verifier="native_response_guard",
                    )
                    self.memory.add_task_event(
                        task_id, "completed" if success else ("waiting_for_approval" if waiting_for_approval else (authorization_terminal or "failed")),
                        f"Paso {idx}: {'completado' if success else ('esperando autorización' if waiting_for_approval else (authorization_terminal or 'fallido'))}.",
                        {"step_index": idx, "attempts": attempts, "tool_calls": total_tool_calls},
                    )
                except Exception:
                    pass

            if success:
                completed_defs.append(dict(step))
                pointer += 1
                continue

            if waiting_for_approval:
                limit_reason = "waiting_for_approval"
                break
            if authorization_terminal:
                limit_reason = "authorization_" + authorization_terminal
                break

            if auto_replan and replans < max_replans and total_tool_calls < max_tool_calls:
                new_steps = self._replan(goal, step, completed_defs, start_index=idx)
                if new_steps:
                    replans += 1
                    steps = steps[:pointer] + new_steps
                    plan = {**plan, "steps": steps, "replans": replans}
                    if task_id and self.memory is not None:
                        try:
                            self.memory.update_task_plan(task_id, plan)
                            self.memory.replace_task_steps(task_id, idx, new_steps)
                            self.memory.add_task_event(task_id, "replanned", f"Plan revisado tras fallo del paso {idx}.", {"replan": replans})
                        except Exception:
                            pass
                    continue

            if stop_on_failed:
                break
            pointer += 1

        cancelled = self._cancel.is_set()
        if limit_reason and limit_reason != "waiting_for_approval" and task_id and self.memory is not None:
            try:
                self.memory.add_task_event(task_id, "blocked", f"Límite de autonomía alcanzado: {limit_reason}.", {"tool_calls": total_tool_calls})
            except Exception:
                pass
        success_count = sum(1 for x in results if x.get("success"))
        ok = pointer >= len(steps) and not cancelled and not limit_reason and bool(steps)
        authorization_status = limit_reason.removeprefix("authorization_") if limit_reason.startswith("authorization_") else ""
        status = "cancelled" if cancelled else ("waiting_for_approval" if limit_reason == "waiting_for_approval" else (authorization_status or ("blocked" if limit_reason else ("completed" if ok else "failed"))))
        summary = f"{success_count} pasos correctos · {replans} replans · {total_tool_calls} tool calls."
        if limit_reason and limit_reason != "waiting_for_approval" and not authorization_status:
            summary += f" Límite: {limit_reason}."
        elif limit_reason == "waiting_for_approval":
            summary += " Esperando autorización local; no se reintentó ni replanteó el paso."
        elif authorization_status:
            summary += f" Autorización {authorization_status}; no se reintentó ni replanteó el paso."
        if task_id and self.memory is not None:
            try:
                self.memory.update_task(task_id, status=status, summary=summary)
            except Exception:
                pass
        if status != "waiting_for_approval":
            self.current_task_id = None
            tools = getattr(self.agent, "tools", None)
            if tools is not None and hasattr(tools, "action_task_id"):
                tools.action_task_id = ""

        elapsed = time.monotonic() - started
        if elapsed >= 8.0:
            try:
                from .runtime_lifecycle import get_current_lifecycle
                lifecycle = get_current_lifecycle()
                tray = getattr(lifecycle, "tray", None) if lifecycle is not None else None
                if lifecycle is not None and lifecycle.window_hidden and tray is not None:
                    tray.notify(
                        "long_task_completed",
                        "Tarea completada",
                        "Nova terminó una tarea larga mientras estaba en segundo plano.",
                    )
            except Exception:
                pass

        return {
            "ok": ok, "task_id": task_id, "status": status, "summary": summary,
            "plan": plan, "steps": results, "replans": replans,
            "tool_calls": total_tool_calls, "limit_reason": limit_reason or None,
        }

    run_task = run
    execute = run
    execute_task = run


class AutonomyEngine(TaskEngine):
    """Contrato histórico: usa el mismo núcleo acotado de TaskEngine."""

    pass
