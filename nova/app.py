import sys
import traceback
from pathlib import Path
import tkinter as tk

# Desde v0.9.0 Agent, Tools, UI y Task Engine también están administrados por
# GitHub. core_runtime conserva temporalmente los adaptadores por dominio para
# migrar comportamiento sin romper compatibilidad en una sola Release.
from assistant.core_runtime import install_core_runtime

install_core_runtime()

from assistant.config import load_config
from assistant.ui import AssistantUI


def main():
    if sys.platform != "win32":
        print("Esta versión está preparada específicamente para Windows.")
        return 1
    root = tk.Tk()
    AssistantUI(root, load_config())
    root.mainloop()
    return 0


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
