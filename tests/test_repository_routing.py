from __future__ import annotations

import unittest

from assistant.agent_repository import repository_route


class RepositoryRoutingTests(unittest.TestCase):
    def test_required_phrases_route_without_llm(self):
        expected = {
            "qué versión eres": "version", "qué versión tienes": "version",
            "qué cambió en la nueva versión": "changes", "qué se agregó en esta actualización": "changes",
            "hay una actualización disponible": "version", "consulta tu repositorio": "activity",
            "cuáles son tus últimos cambios": "changes", "muéstrame tu changelog": "changes",
            "estado de tu repo": "activity",
        }
        for phrase, route in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(repository_route(phrase), route)


if __name__ == "__main__":
    unittest.main()
