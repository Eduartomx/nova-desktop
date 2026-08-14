from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.test_recovery_bootstrap import RecoveryFixture
from updater.recovery_bootstrap import recover_pending
from updater.recovery_state import load_journal, transition_journal


class RecoveryWindowsDetailTests(unittest.TestCase):
    def test_restore_detail(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            transition_journal(
                fx.root, journal, "files_applying",
                backup_root=fx.backup_root, files_may_have_changed=True,
            )
            result = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                validator=lambda *_args: (True, "validated"),
                launch_after_success=False,
            )
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertTrue(result.recovered, f"result={result!r}; journal={current!r}")


if __name__ == "__main__":
    unittest.main()
