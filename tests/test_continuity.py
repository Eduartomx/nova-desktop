from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assistant.agent_continuity import continuity_direct_intent
from assistant.memory import MemoryStore
from assistant.workspace import WorkspaceManager


class ContinuityTests(unittest.TestCase):
    def make_store(self, root: Path):
        store = MemoryStore(root / "data" / "assistant.db")
        store.configure_continuity({
            "enabled": True,
            "auto_checkpoint_tasks": True,
            "inject_context": True,
        })
        return store

    def test_task_creates_and_updates_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            store = self.make_store(root)
            ws = WorkspaceManager(store).create(str(project), name="Proyecto Continuidad")

            task_id = store.create_task(
                "Resolver crash del servidor",
                {"steps": [{"index": 1, "description": "Revisar latest.log"}, {"index": 2, "description": "Probar sin Mod X"}]},
                status="running",
            )
            state = store.continuity_resume(workspace_id=ws["id"])
            self.assertTrue(state["ok"])
            self.assertEqual(state["session"]["task_id"], task_id)
            self.assertIn("Revisar latest.log", state["checkpoint"]["pending"])

            store.upsert_task_step(task_id, 1, "Revisar latest.log", "Encontrar causa", status="completed", result="Conflicto posible")
            store.upsert_task_step(task_id, 2, "Probar sin Mod X", "Servidor inicia", status="pending")
            store.update_task(task_id, status="paused", summary="Se encontró un conflicto probable; falta probar sin Mod X")

            resumed = store.continuity_resume(workspace_id=ws["id"])
            cp = resumed["checkpoint"]
            self.assertIn("Se encontró un conflicto probable", cp["summary"])
            self.assertTrue(any("Revisar latest.log" in item for item in cp["completed"]))
            self.assertTrue(any("Probar sin Mod X" in item for item in cp["pending"]))
            self.assertEqual(resumed["session"]["status"], "paused")

            pending = store.continuity_pending(workspace_id=ws["id"])
            self.assertTrue(any("Probar sin Mod X" in item for item in pending["pending_items"]))

    def test_manual_checkpoint_survives_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            store = self.make_store(root)
            ws = WorkspaceManager(store).create(str(project), name="Proyecto Manual")
            store.continuity_checkpoint(
                workspace_id=ws["id"],
                summary="Configuración revisada",
                completed=["Revisar config"],
                pending=["Ejecutar prueba final"],
                files=["config.json"],
                decisions=["Mantener modo trusted"],
                errors=["Prueba anterior falló"],
                kind="manual",
                session_status="active",
            )
            del store

            reopened = self.make_store(root)
            state = reopened.continuity_resume(workspace_id=ws["id"])
            self.assertTrue(state["ok"])
            self.assertEqual(state["checkpoint"]["summary"], "Configuración revisada")
            self.assertEqual(state["checkpoint"]["files"], ["config.json"])
            self.assertIn("Ejecutar prueba final", state["compact"])
            stats = reopened.stats()
            self.assertGreaterEqual(stats["continuity_sessions"], 1)
            self.assertGreaterEqual(stats["continuity_checkpoints"], 1)

    def test_close_session(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            store = self.make_store(root)
            ws = WorkspaceManager(store).create(str(project), name="Proyecto Cierre")
            store.continuity_checkpoint(workspace_id=ws["id"], summary="Trabajo en progreso", pending=["Terminar"])
            result = store.continuity_close(workspace_id=ws["id"], status="completed", summary="Todo terminado")
            self.assertTrue(result["ok"])
            self.assertEqual(result["session"]["status"], "completed")
            latest = store.continuity_resume(workspace_id=ws["id"])
            self.assertEqual(latest["checkpoint"]["summary"], "Todo terminado")

    def test_direct_routing(self):
        self.assertEqual(continuity_direct_intent("Nova, continúa con lo de ayer"), "continue")
        self.assertEqual(continuity_direct_intent("¿Dónde nos quedamos?"), "status")
        self.assertEqual(continuity_direct_intent("¿Qué quedó pendiente?"), "pending")
        self.assertEqual(continuity_direct_intent("¿Qué hicimos ayer?"), "history")
        self.assertIsNone(continuity_direct_intent("Continúa buscando archivos en Internet"))


if __name__ == "__main__":
    unittest.main()
