from __future__ import annotations

"""Task Engine nativo de Nova.

La implementación 0.9.0 conserva el contrato histórico (TaskEngine /
AutonomyEngine) y delega ejecución real al Agent/Tools. No concede permisos ni
ejecuta código fuera de las políticas normales.
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
        self._pause.clear()
        self.current_task_id: int | None = None

    @property
    def settings(self) -> dict[str, Any]:
        cfg = self.config.get("task_engine", {}) if isinstance(self.config, dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def cancel(self):
        self._cancel.set()
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
        # El Planner pide estructura pero no ejecuta nada. Si el modelo no entrega
        # JSON válido, usamos un plan seguro de un solo paso en vez de inventar.
        prompt = (
            "Actúa solo como Planner. NO ejecutes herramientas. Devuelve únicamente JSON con formato "
            '{"steps":[{"description":"...","success_criteria":"..."}]}. '
            f"Máximo {max_steps} pasos concretos y verificables. Objetivo: {goal}"
        )
        try:
            messages = [
                {"role": "system", "content": "Eres el Planner determinista de Nova. Solo produces un plan JSON; no ejecutas acciones."},
                {"role": "user", "content": prompt},
            ]
            data = self.agent._ollama_chat(messages, tools=None)
            content = str((data.get("message") or {}).get("content") or "")
            parsed = self._extract_json(content) or {}
            steps = []
            for idx, row in enumerate(list(parsed.get("steps") or [])[:max_steps], start=1):
                if isinstance(row, str):
                    row = {"description": row}
                if not isinstance(row, dict):
                    continue
                description = str(row.get("description") or "").strip()
                if not description:
                    continue
                steps.append({
                    "index": idx,
                    "description": description[:1200],
                    "success_criteria": str(row.get("success_criteria") or "Resultado comprobado y consistente con el objetivo.")[:800],
                })
            if steps:
                return {"goal": goal, "steps": steps, "planner": "ollama"}
        except Exception:
            pass
        return {
            "goal": goal,
            "steps": [{"index": 1, "description": goal, "success_criteria": "El objetivo queda completado y verificado."}],
            "planner": "safe_fallback",
        }

    def _wait_if_paused(self):
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(max(0.05, float(self.settings.get("pause_poll_ms", 150) or 150) / 1000.0))

    @staticmethod
    def _response_success(text: str) -> bool:
        t = str(text or "").casefold()
        negative = (
            "no pude", "no se pudo", "falló", "fallo", "error:", "confirmation_required",
            "alcancé el límite", "no pude conectar con ollama",
        )
        return bool(t.strip()) and not any(x in t for x in negative)

    def run(self, goal: str, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.settings.get("enabled", True):
            return {"ok": False, "error": "task_engine_disabled"}
        self._cancel.clear()
        self._pause.clear()
        plan = plan or self.plan(goal)
        steps = list(plan.get("steps") or [])
        if not steps:
            return {"ok": False, "error": "empty_plan"}

        task_id = None
        if self.memory is not None:
            try:
                task_id = self.memory.create_task(str(goal), plan, status="running")
                self.current_task_id = int(task_id)
                for idx, step in enumerate(steps, start=1):
                    self.memory.upsert_task_step(
                        task_id, idx, str(step.get("description") or ""),
                        str(step.get("success_criteria") or ""), status="pending",
                    )
                self.memory.add_task_event(task_id, "started", "Task Engine inició la ejecución.")
            except Exception:
                task_id = None

        results = []
        max_retries = max(0, min(int(self.settings.get("max_step_retries", 1) or 1), 3))
        stop_on_failed = bool(self.settings.get("stop_on_failed_step", True))

        for idx, step in enumerate(steps, start=1):
            self._wait_if_paused()
            if self._cancel.is_set():
                break
            description = str(step.get("description") or "").strip()
            criteria = str(step.get("success_criteria") or "").strip()
            attempts = 0
            success = False
            response = ""
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
                    f"Paso {idx}/{len(steps)}: {description}\n"
                    f"Criterio de éxito: {criteria or 'resultado verificable'}\n"
                    "Usa herramientas reales si hacen falta y no afirmes éxito sin evidencia."
                )
                response = str(self.agent.ask(instruction) or "")
                success = self._response_success(response)
                if success:
                    break
            results.append({"index": idx, "success": success, "attempts": attempts, "result": response})
            if task_id and self.memory is not None:
                try:
                    self.memory.upsert_task_step(
                        task_id, idx, description, criteria,
                        status="completed" if success else "failed",
                        attempts=attempts,
                        result=response[:5000],
                        verifier="native_response_guard",
                    )
                    self.memory.add_task_event(
                        task_id, "completed" if success else "failed",
                        f"Paso {idx}: {'completado' if success else 'fallido'}.",
                        {"step_index": idx, "attempts": attempts},
                    )
                except Exception:
                    pass
            if not success and stop_on_failed:
                break

        cancelled = self._cancel.is_set()
        ok = bool(results) and all(row.get("success") for row in results) and len(results) == len(steps) and not cancelled
        status = "cancelled" if cancelled else ("completed" if ok else "failed")
        summary = f"{sum(1 for x in results if x.get('success'))}/{len(steps)} pasos completados."
        if task_id and self.memory is not None:
            try:
                self.memory.update_task(task_id, status=status, summary=summary)
            except Exception:
                pass
        self.current_task_id = None
        return {"ok": ok, "task_id": task_id, "status": status, "summary": summary, "plan": plan, "steps": results}

    # Contratos históricos usados por UI/profiler.
    run_task = run
    execute = run
    execute_task = run


class AutonomyEngine(TaskEngine):
    """Alias compatible: v0.9 mantiene una sola implementación de ejecución."""

    pass
