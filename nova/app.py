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
    # Esta comprobación ocurre antes de core_runtime/AssistantUI: una segunda
    # ejecución no construye Agent, no registra hotkeys y no precarga Qwen.
    from assistant.instance_commands import InstanceCommandMailbox
    from assistant.instance_lock import InstanceLock, runtime_directory

    directory = runtime_directory()
    lock = InstanceLock(path=directory / "nova.lock")
    mailbox = InstanceCommandMailbox(directory / "nova.command")
    if not lock.acquire():
        mailbox.send("show")
        return None, mailbox
    mailbox.clear()
    return lock, mailbox


def main(argv=None):
    if sys.platform != "win32":
        print("Esta versión está preparada específicamente para Windows.")
        return 1

    args = _arguments(argv)
    instance_lock, command_mailbox = _claim_instance()
    if instance_lock is None:
        # La instancia existente recibió la orden local `show`.
        return 0

    root = None
    try:
        # El núcleo pesado solo se instala después de adquirir la instancia.
        from assistant.core_runtime import install_core_runtime
        install_core_runtime()
        from assistant.config import load_config
        from assistant.ui import AssistantUI

        root = tk.Tk()
        AssistantUI(
            root,
            load_config(),
            instance_lock=instance_lock,
            command_mailbox=command_mailbox,
            start_hidden=bool(args.background and not args.post_update),
        )
        root.mainloop()
        return 0
    finally:
        # request_shutdown normalmente libera el lock. Este finally cubre error
        # de arranque, destroy externo o cierre inesperado de mainloop.
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
