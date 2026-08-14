from __future__ import annotations

"""Stable recovery API composed from focused stdlib-only primitives.

The implementation is split by responsibility so the validated fallback bundle
can carry the exact journal, process-identity, file-rollback, and environment
validation code without depending on Nova's assistant stack or third-party
packages. This module is the existing public recovery API, not a second
implementation.
"""

try:
    from .recovery_journal import *
    from .recovery_journal import _JournalLock, _atomic_bytes, _atomic_json, _identity, _json_bytes, _read_raw_journal, _sanitize, _utc_now
    from .recovery_attempts import *
    from .recovery_attempts import _backup_rel
    from .recovery_files import *
    from .recovery_files import _atomic_copy, _install_target, _manifest_lists, _reject_symlink_chain
    from .recovery_environment import *
    from .recovery_environment import (
        _installed_distributions, _load_dependency_snapshot, _normalized_distribution_name,
        _requirements_sha256, _run_critical_imports, _validate_python_files,
    )
except ImportError:
    from recovery_journal import *
    from recovery_journal import _JournalLock, _atomic_bytes, _atomic_json, _identity, _json_bytes, _read_raw_journal, _sanitize, _utc_now
    from recovery_attempts import *
    from recovery_attempts import _backup_rel
    from recovery_files import *
    from recovery_files import _atomic_copy, _install_target, _manifest_lists, _reject_symlink_chain
    from recovery_environment import *
    from recovery_environment import (
        _installed_distributions, _load_dependency_snapshot, _normalized_distribution_name,
        _requirements_sha256, _run_critical_imports, _validate_python_files,
    )
