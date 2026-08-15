from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.test_recovery_bootstrap import RecoveryFixture
from updater.recovery_state import RecoveryJournalError, transition_journal


class RecoveryTerminalStateTests(unittest.TestCase):
    def test_cleared_cannot_transition_back_to_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            journal = transition_journal(
                fx.root, journal, "files_applying",
                backup_root=fx.backup_root, files_may_have_changed=True,
            )
            journal = transition_journal(fx.root, journal, "files_applied", backup_root=fx.backup_root)
            journal = transition_journal(fx.root, journal, "update_validation_in_progress", backup_root=fx.backup_root)
            journal = transition_journal(fx.root, journal, "update_validated", backup_root=fx.backup_root)
            cleared = transition_journal(fx.root, journal, "cleared", backup_root=fx.backup_root)
            with self.assertRaises(RecoveryJournalError):
                transition_journal(
                    fx.root,
                    cleared,
                    "rollback_validation_completed",
                    backup_root=fx.backup_root,
                )


if __name__ == "__main__":
    unittest.main()
