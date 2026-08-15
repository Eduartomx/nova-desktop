from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from tests.test_recovery_bootstrap import RecoveryFixture
from updater.recovery_bootstrap import recover_pending
from updater.recovery_handoff import stable_bootstrap_path
from updater.recovery_state import load_journal


class PreMutationStableBootstrapTests(unittest.TestCase):
    def test_transaction_prepared_crash_rebuilds_stable_bundle_before_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            fx = RecoveryFixture(Path(td))
            journal = fx.transaction()
            self.assertEqual(journal["state"], "transaction_prepared")
            self.assertFalse(journal["files_may_have_changed"])

            shutil.rmtree(fx.root / "data" / "recovery_runtime")
            launches = []
            result = recover_pending(
                fx.root,
                backup_root=fx.backup_root,
                validator=lambda *_args: (True, "validated"),
                launcher=lambda command, **kwargs: launches.append((list(command), kwargs)) or object(),
                launch_after_success=True,
            )

            self.assertTrue(result.recovered, result)
            self.assertTrue(result.launched, result)
            self.assertEqual(len(launches), 1)
            self.assertIn("--handoff-launch", launches[0][0])
            self.assertTrue(stable_bootstrap_path(fx.root).is_file())
            current = load_journal(fx.root, backup_root=fx.backup_root)
            self.assertEqual(current["state"], "cleared")
            self.assertFalse(current["recovery_required"])


if __name__ == "__main__":
    unittest.main()
