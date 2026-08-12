from __future__ import annotations

import re
import unicodedata
from typing import Any


def _normalize_command(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").casefold())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", raw).strip()


def semantic_direct_intent(text: str) -> tuple[str | None, dict[str, Any]]:
    """Detecta órdenes inequívocas de Semantic Memory antes del LLM/Task Engine."""
    t = _normalize_command(text)
    semantic = (
        "memoria semantica" in t
        or "semantic memory" in t
        or "embedding" in t
        or "embeddings" in t
    )
    memory_reindex = "memoria" in t and any(x in t for x in ("reindex", "re-index", "regenera", "reconstruye"))

    if semantic or memory_reindex:
        reindex_cues = (
            "reindex", "re-index", "regenera", "reconstruye",
            "indexa la memoria", "indexar la memoria",
            "actualiza los embeddings", "actualiza embeddings",
            "genera los embeddings", "genera embeddings",
        )
        if any(cue in t for cue in reindex_cues):
            workspace_only = any(
                cue in t
                for cue in (
                    "workspace actual", "proyecto actual", "este proyecto",
                    "solo este proyecto", "solo el proyecto", "del workspace",
                )
            )
            return "reindex", {"force": True, "workspace_only": workspace_only}

        status_cues = (
            "estado", "status", "funciona", "funcionando", "activa", "activo",
            "disponible", "modelo", "cuantos", "cuantas", "indexados", "indexadas",
        )
        if any(cue in t for cue in status_cues):
            return "status", {"refresh": True}

    return None, {}


def _format_reindex_result(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        detail = str(result.get("detail") or result.get("error") or "No se pudo reindexar Semantic Memory.")
        command = str(result.get("install_command") or "").strip()
        text = f"No pude reindexar la memoria semántica: {detail}"
        if command:
            text += f"\n\nPara instalar el modelo local necesario ejecuta:\n{command}"
        return text

    indexed = int(result.get("indexed") or 0)
    skipped = int(result.get("skipped") or 0)
    candidates = int(result.get("candidates") or indexed + skipped)
    model = str(result.get("model") or "modelo configurado")
    workspace = result.get("workspace")
    detail = str(result.get("detail") or "Índice semántico actualizado")
    scope = f" · workspace: {workspace}" if workspace else " · memoria global"
    return (
        "Memoria semántica reindexada correctamente.\n"
        f"Modelo: {model}{scope}\n"
        f"Procesadas: {candidates} · regeneradas: {indexed} · sin cambios: {skipped}\n"
        f"{detail}"
    )


def _format_status_result(result: dict[str, Any]) -> str:
    status = result.get("status") if isinstance(result.get("status"), dict) else result
    enabled = bool(status.get("enabled"))
    available = bool(status.get("model_available"))
    model = str(status.get("model") or "modelo no configurado")
    indexed = int(status.get("indexed") or 0)
    pending = int(status.get("pending") or 0)
    total = int(status.get("total_candidates") or indexed + pending)
    detail = str(status.get("detail") or "")
    workspace = result.get("workspace") or status.get("workspace")
    scope = f" · workspace: {workspace}" if workspace else ""
    if enabled and available:
        return (
            f"Semantic Memory está activa{scope}.\n"
            f"Modelo: {model}\n"
            f"Índice: {indexed}/{total} recuerdos · pendientes: {pending}.\n"
            f"{detail}"
        )
    command = str(status.get("install_command") or "").strip()
    text = f"Semantic Memory no está disponible ahora{scope}.\nModelo: {model}\n{detail}"
    if command:
        text += f"\n\nPara instalar el modelo local:\n{command}"
    return text


def install_agent_v063():
    from . import agent as mod

    Agent = mod.LocalAgent
    if getattr(Agent, "_nova_v063_patched", False):
        return mod

    original_init = Agent.__init__
    original_ask = Agent.ask
    original_prompt = getattr(Agent, "_system_prompt", None)

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            self.memory.configure_semantic_memory(
                self.config.get("semantic_memory", {}),
                self.config.get("ollama_host", "http://127.0.0.1:11434"),
            )
        except Exception:
            pass

    def ask(self, user_text):
        action, params = semantic_direct_intent(user_text)
        if action:
            # Mantiene coherente el contexto de la capa v0.6 aunque no invoquemos al LLM.
            try:
                self._current_user_text = user_text or ""
            except Exception:
                pass
            try:
                tools = getattr(self, "tools", None)
                if action == "reindex":
                    if tools is not None and hasattr(tools, "memory_semantic_reindex"):
                        result = tools.memory_semantic_reindex(**params)
                    else:
                        active = self.memory.active_workspace() if params.get("workspace_only") else None
                        wid = int(active["id"]) if active else None
                        result = self.memory.semantic_reindex(workspace_id=wid, force=bool(params.get("force", True)))
                        result["workspace"] = active.get("name") if active else None
                        if not result.get("ok"):
                            result["install_command"] = self.memory.semantic_status(workspace_id=wid).get("install_command")
                    return _format_reindex_result(result)

                if tools is not None and hasattr(tools, "memory_semantic_status"):
                    result = tools.memory_semantic_status(**params)
                else:
                    result = self.memory.semantic_status(refresh=bool(params.get("refresh", True)))
                return _format_status_result(result)
            except Exception as exc:
                return f"No pude ejecutar la operación de Semantic Memory: {exc}"

        return original_ask(self, user_text)

    def system_prompt(self):
        base = original_prompt(self) if callable(original_prompt) else ""
        return base + """

SEMANTIC MEMORY — ROUTING CRÍTICO
- «reindexa/regenera/reconstruye la memoria semántica» significa memory_semantic_reindex, nunca Windows Search, SearchIndexer, PowerShell ni servicios de Windows.
- «estado/funciona la memoria semántica» significa memory_semantic_status.
- Si falta el modelo de embeddings, informa el comando ollama pull indicado por la herramienta; no inventes una vía alternativa del sistema operativo.
"""

    Agent.__init__ = init
    Agent.ask = ask
    if callable(original_prompt):
        Agent._system_prompt = system_prompt
    Agent._nova_v063_patched = True
    return mod
