from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
NOVA = REPO / "nova"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


class UpdaterEntrypointGuardTests(unittest.TestCase):
    def _run_direct(self, script_name: str):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            (root / "data").mkdir(parents=True)
            (root / "app.py").write_text("# sentinel\n", encoding="utf-8")
            (root / "data" / "sentinel.bin").write_bytes(b"unchanged")
            before = _tree(root)
            env = os.environ.copy()
            env["NOVA_HOME"] = str(root)
            env["PATH"] = ""
            proc = subprocess.run(
                [sys.executable, str(NOVA / "updater" / script_name), "--yes"],
                cwd=str(NOVA), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=15, shell=False,
            )
            after = _tree(root)
        return proc, before, after

    def test_resident_engine_direct_yes_is_inert(self):
        proc, before, after = self._run_direct("resident_update_engine.py")
        self.assertEqual(proc.returncode, 4, proc.stdout)
        self.assertIn("import-only", proc.stdout)
        self.assertEqual(after, before)
        self.assertNotIn("INSTALADA DESDE GITHUB", proc.stdout)

    def test_legacy_engine_direct_yes_is_inert(self):
        proc, before, after = self._run_direct("nova_updater_legacy.py")
        self.assertEqual(proc.returncode, 4, proc.stdout)
        self.assertIn("import-only", proc.stdout)
        self.assertEqual(after, before)
        self.assertNotIn("INSTALADA DESDE GITHUB", proc.stdout)


if __name__ == "__main__":
    unittest.main()
