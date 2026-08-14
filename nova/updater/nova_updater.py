from __future__ import annotations

"""Supported Nova updater entry point.

All update requests, including the historical ``--yes`` flag, are delegated to
``update_runner.py`` so the supervisor mutex and persistent recovery gate cannot
be bypassed through this public script. The transactional implementation is
kept in ``nova_updater_legacy`` for the hardened resident engine and backwards
compatible tests/imports.
"""

import argparse
from pathlib import Path
import subprocess
import sys
import types

try:
    from . import nova_updater_legacy as _legacy
except ImportError:
    import nova_updater_legacy as _legacy

# Re-export the established updater API. Function objects retain their legacy
# globals; the module proxy below mirrors monkey-patched/configuration values to
# that module so existing callers and tests keep the same behavior.
for _name in dir(_legacy):
    if _name.startswith("__"):
        continue
    if _name not in globals():
        globals()[_name] = getattr(_legacy, _name)


class _UpdaterProxyModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if not name.startswith("__") and hasattr(_legacy, name):
            setattr(_legacy, name, value)


def _delegate_direct_update() -> int:
    root = Path(ROOT)
    runner = root / "updater" / "update_runner.py"
    if not runner.exists():
        print("[ERROR] Falta updater/update_runner.py; actualización cancelada.")
        return 4
    py = Path(sys.executable)
    if py.name.casefold() == "pythonw.exe":
        console = py.with_name("python.exe")
        if console.exists():
            py = console
    print("Delegando la actualización al supervisor de ciclo de vida de Nova…")
    try:
        return int(subprocess.call([str(py), str(runner)], cwd=str(root)))
    except Exception as exc:
        print(f"[ERROR] No pude iniciar el supervisor: {type(exc).__name__}: {exc}")
        return 4


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Actualizador GitHub-native de Nova")
    parser.add_argument("--check", action="store_true", help="Solo comprobar si hay actualización")
    parser.add_argument("--yes", action="store_true", help="Compatibilidad: sigue delegando al supervisor seguro")
    args, _unknown = parser.parse_known_args(argv)
    if args.check:
        return int(check_only())
    # No supported invocation of nova_updater.py applies files directly.
    return _delegate_direct_update()


sys.modules[__name__].__class__ = _UpdaterProxyModule


if __name__ == "__main__":
    raise SystemExit(main())
