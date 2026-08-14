import argparse
import sys
import traceback
from pathlib import Path
import tkinter as tk


def _arguments(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--post-update", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    return args


def _claim_instance():
    """Claim the scoped runtime before importing the core or constructing Tk.

    Returns ``(lock, mailbox, exit_code)``. A secondary launch never creates
    Agent/hotkeys/services; it only targets ``show`` to the exact owner
    generation that currently holds the kernel lock.
    """
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
    """Best-effort cleanup when Tk/mainloop itself failed.

    The scheduled and synchronous shutdown attempts are intentionally isolated:
    a broken ``root.after`` must never suppress the synchronous cleanup path.
    """
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

    args = _arguments(argv)
    instance_lock, command_mailbox, secondary_code = _claim_instance()
    if instance_lock is None:
        return int(secondary_code)

    root = None
    ui = None
    exit_code = 0
    try:
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
            start_hidden=bool(args.background and not args.post_update),
        )
        try:
            root.mainloop()
        except BaseException:
            # A Tk/runtime error is a real shutdown, never a hide-to-tray action.
            _cleanup_runtime_error(ui)
            exit_code = 1
            raise
        return exit_code
    finally:
        # The ownership lock deliberately outlives Tk destruction and every
        # lifecycle cleanup step. The updater additionally waits for the owning
        # process handle, so releasing here can never by itself authorize writes.
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
