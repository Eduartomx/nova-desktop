from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from updater.process_launch import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    DETACHED_PROCESS,
    detached_hidden_creation_flags,
    hidden_supervisor_creation_flags,
    select_console_python,
    select_gui_python,
)


class ProcessLaunchTests(unittest.TestCase):
    @staticmethod
    def _file(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def test_managed_environment_wins_for_console_and_gui(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            console = self._file(root / ".venv" / "Scripts" / "python.exe")
            gui = self._file(root / ".venv" / "Scripts" / "pythonw.exe")
            external = Path(td) / "external" / "python.exe"
            self.assertEqual(select_console_python(root, external), console)
            self.assertEqual(select_gui_python(root, external), gui)

    def test_external_environment_uses_only_current_siblings(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "nova"
            console = self._file(Path(td) / "external" / "Scripts" / "python.exe")
            gui = self._file(console.with_name("pythonw.exe"))
            self.assertEqual(select_gui_python(root, console), gui)
            self.assertEqual(select_console_python(root, gui), console)

    def test_current_pythonw_is_preserved_without_managed_environment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "nova"
            current = Path(td) / "external" / "Scripts" / "pythonw.exe"
            self.assertEqual(select_gui_python(root, current), current)

    def test_absent_gui_falls_back_to_controlled_console_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "nova"
            current = Path(td) / "external" / "Scripts" / "python.exe"
            self.assertEqual(select_console_python(root, current), current)
            self.assertEqual(select_gui_python(root, current), current)

    def test_windows_profiles_are_hidden_and_never_create_a_new_console(self):
        supervisor = hidden_supervisor_creation_flags("nt")
        detached = detached_hidden_creation_flags("nt")
        self.assertEqual(supervisor, CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(detached, DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(supervisor & 0x00000010, 0)  # CREATE_NEW_CONSOLE
        self.assertEqual(detached & 0x00000010, 0)
        self.assertEqual(hidden_supervisor_creation_flags("posix"), 0)
        self.assertEqual(detached_hidden_creation_flags("posix"), 0)


if __name__ == "__main__":
    unittest.main()
