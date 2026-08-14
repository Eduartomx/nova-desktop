import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback

RECOVERY_REQUIRED_EXIT_CODE = 7


def _arguments(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--post-update", action="store_true")
    parser.add_argument("--post-recovery", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    return args


def _safe_recovery_state(root: Path) -> str:
    try:
        path = Path(root) / "data" / "update_recovery.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return str(payload.get("state") or payload.get("status") or "unknown")[:80]
    except Exception:
        pass
    return "unknown"


def _stable_runtime_paths(root: Path) -> tuple[Path, dict]:
    runtime = Path(root) / "data" / "recovery_runtime"
    active = runtime / "active.json"
    if not active.is_file() or active.is_symlink():
        raise RuntimeError("stable_recovery_active_missing")
    pointer_bytes = active.read_bytes()
    try:
        pointer = json.loads(pointer_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"stable_recovery_active_corrupt:{type(exc).__name__}") from exc
    if not isinstance(pointer, dict) or int(pointer.get("schema_version") or 0) != 1:
        raise RuntimeError("stable_recovery_active_schema")
    generation = str(pointer.get("generation") or "")
    if len(generation) != 32 or any(ch not in "0123456789abcdef" for ch in generation.lower()):
        raise RuntimeError("stable_recovery_generation_invalid")
    generations = runtime / "generations"
    target = generations / generation
    if generations.is_symlink() or target.is_symlink() or not target.is_dir():
        raise RuntimeError("stable_recovery_generation_unavailable")
    base = generations.resolve(strict=True)
    resolved = target.resolve(strict=True)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise RuntimeError("stable_recovery_generation_escape") from exc
    manifest_path = resolved / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError("stable_recovery_manifest_missing")
    manifest_bytes = manifest_path.read_bytes()
    expected_manifest = str(pointer.get("manifest_sha256") or "").lower()
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest:
        raise RuntimeError("stable_recovery_manifest_hash_mismatch")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"stable_recovery_manifest_corrupt:{type(exc).__name__}") from exc
    if not isinstance(manifest, dict) or int(manifest.get("schema_version") or 0) != 1 or manifest.get("generation") != generation:
        raise RuntimeError("stable_recovery_manifest_invalid")
    files = manifest.get("files")
    expected_files = {
        "recovery_journal.py", "recovery_attempts.py", "recovery_files.py", "recovery_environment.py",
        "recovery_state.py", "recovery_locking.py", "recovery_bootstrap.py",
    }
    if not isinstance(files, dict) or set(files) != expected_files:
        raise RuntimeError("stable_recovery_files_invalid")
    for name, expected in files.items():
        path = resolved / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"stable_recovery_file_invalid:{name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != str(expected or "").lower():
            raise RuntimeError(f"stable_recovery_hash_mismatch:{name}")
    return resolved, manifest


def _load_stable_recovery_bootstrap(root: Path):
    """Load only a hash-validated stdlib recovery generation from data/."""
    generation_dir, _manifest = _stable_runtime_paths(root)
    state_path = generation_dir / "recovery_state.py"
    bootstrap_path = generation_dir / "recovery_bootstrap.py"
    module_names = (
        "recovery_journal", "recovery_attempts", "recovery_files", "recovery_environment",
        "recovery_state", "recovery_locking", "recovery_bootstrap",
    )
    old_modules = {name: sys.modules.get(name) for name in module_names}
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(generation_dir))
        state_spec = importlib.util.spec_from_file_location("recovery_state", state_path)
        if state_spec is None or state_spec.loader is None:
            raise RuntimeError("stable_recovery_state_spec_failed")
        state_module = importlib.util.module_from_spec(state_spec)
        sys.modules["recovery_state"] = state_module
        state_spec.loader.exec_module(state_module)

        bootstrap_spec = importlib.util.spec_from_file_location("recovery_bootstrap", bootstrap_path)
        if bootstrap_spec is None or bootstrap_spec.loader is None:
            raise RuntimeError("stable_recovery_bootstrap_spec_failed")
        bootstrap_module = importlib.util.module_from_spec(bootstrap_spec)
        sys.modules["recovery_bootstrap"] = bootstrap_module
        bootstrap_spec.loader.exec_module(bootstrap_module)
        return bootstrap_module
    finally:
        sys.path[:] = old_path
        for name in module_names:
            previous = old_modules[name]
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _native_recovery_notice(root: Path, *, state: str = "unknown", detail: str = "") -> None:
    root = Path(root)
    data = root / "data"
    stable_command = "python updater/recovery_bootstrap.py --status"
    try:
        generation, _manifest = _stable_runtime_paths(root)
        stable_command = f'python "{generation / "recovery_bootstrap.py"}" --status --root "{root}"'
    except Exception:
        pass
    message = (
        "Nova detectó una recuperación bloqueada y no iniciará el asistente normal.\n\n"
        f"Estado: {str(state or 'unknown')[:80]}\n"
        f"Detalle: {str(detail or 'bootstrap de recuperación no disponible')[:220]}\n\n"
        f"Datos y backup: {data}\n"
        "No borres update_recovery.json ni el backup.\n\n"
        f"Diagnóstico: {stable_command}\n"
        "Cuando el diagnóstico indique que es seguro, usa el mismo bootstrap con --recover."
    )
    if os.name == "nt" or sys.platform == "win32":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
            user32.MessageBoxW.restype = ctypes.c_int
            user32.MessageBoxW(None, message, "Nova · Recuperación bloqueada", 0x00000030)
            return
        except Exception:
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def _interpret_recovery_result(result) -> tuple[bool, int]:
    if bool(getattr(result, "continue_startup", False)):
        return True, 0
    recovered = bool(getattr(result, "recovered", False))
    launched = bool(getattr(result, "launched", False))
    code = int(getattr(result, "exit_code", 0) or (0 if recovered and launched else RECOVERY_REQUIRED_EXIT_CODE))
    return False, code


def _startup_recovery_gate(argv=None):
    """Run before instance ownership, Tk, Agent, or assistant imports."""
    root = Path(__file__).resolve().parent
    journal = root / "data" / "update_recovery.json"
    try:
        from updater.recovery_bootstrap import startup_recovery_gate
        result = startup_recovery_gate(root)
        return _interpret_recovery_result(result)
    except Exception as managed_error:
        if not journal.exists():
            return True, 0
        try:
            stable = _load_stable_recovery_bootstrap(root)
            result = stable.startup_recovery_gate(root)
            return _interpret_recovery_result(result)
        except Exception as stable_error:
            state = _safe_recovery_state(root)
            _native_recovery_notice(
                root,
                state=state,
                detail=f"managed={type(managed_error).__name__}; stable={type(stable_error).__name__}",
            )
            return False, RECOVERY_REQUIRED_EXIT_CODE


def _claim_instance():
    """Claim the scoped runtime before importing the core or constructing Tk."""
    from assistant.instance_commands import InstanceCommandMailbox
    from assistant.instance_lock import InstanceLock, runtime_paths

    paths = runtime_paths()
    lock = InstanceLock(path=paths.lock, owner_path=paths.owner)
    if lock.acquire():
        mailbox = InstanceCommandMailbox(paths.commands, owner_id=lock.owner_id)
        mailbox.purge_foreign(owner_id=lock.owner_id)
        return lock, mailbox, 0

    owner = lock.read_owner()
    if not owner:
        print("Nova: existe un lock ocupado, pero no pude identificar de forma segura a su propietario.", file=sys.stderr)
        return None, None, 3
    mailbox = InstanceCommandMailbox(paths.commands)
    if not mailbox.send("show", target_owner_id=str(owner.get("owner_id") or "")):
        print("Nova: la instancia existente no pudo recibir la orden local 'show'.", file=sys.stderr)
        return None, None, 4
    return None, mailbox, 0


def _cleanup_runtime_error(ui) -> None:
    lifecycle = getattr(ui, "runtime_lifecycle", None) if ui is not None else None
    if lifecycle is None:
        return
    try:
        lifecycle.request_shutdown("runtime_error")
    except Exception:
        pass
    try:
        lifecycle.perform_shutdown_now()
    except Exception:
        pass


def main(argv=None):
    if sys.platform != "win32":
        print("Esta versión está preparada específicamente para Windows.")
        return 1

    continue_startup, recovery_code = _startup_recovery_gate(argv)
    if not continue_startup:
        return int(recovery_code)

    args = _arguments(argv)
    instance_lock, command_mailbox, secondary_code = _claim_instance()
    if instance_lock is None:
        return int(secondary_code)

    root = None
    ui = None
    exit_code = 0
    try:
        import tkinter as tk
        from assistant.core_runtime import install_core_runtime
        install_core_runtime()
        from assistant.config import load_config
        from assistant.ui import AssistantUI

        root = tk.Tk()
        ui = AssistantUI(
            root,
            load_config(),
            instance_lock=instance_lock,
            command_mailbox=command_mailbox,
            start_hidden=bool(args.background and not args.post_update and not args.post_recovery),
        )
        try:
            root.mainloop()
        except BaseException:
            _cleanup_runtime_error(ui)
            exit_code = 1
            raise
        return exit_code
    finally:
        try:
            instance_lock.release()
        except Exception:
            pass


def report_startup_error(exc: BaseException):
    log_path = Path(__file__).resolve().parent / "nova_error.log"
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        log_path.write_text(details, encoding="utf-8")
    except Exception:
        pass
    try:
        import tkinter as tk
        err_root = tk.Tk()
        err_root.withdraw()
        from tkinter import messagebox
        messagebox.showerror(
            "Nova - error al iniciar",
            f"Nova no pudo iniciar.\n\n{exc}\n\nSe guardó el detalle en:\n{log_path}",
        )
        err_root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        report_startup_error(exc)
        raise SystemExit(1)
