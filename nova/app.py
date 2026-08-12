import sys
import traceback
from pathlib import Path
import tkinter as tk

# v0.6 añade Memory, Workspace Intelligence y Semantic Memory sobre los
# módulos históricos v0.5 mientras completamos la migración del núcleo.
from assistant.v060_runtime import install_v060
from assistant.v061_runtime import install_v061
from assistant.v063_runtime import install_v063

install_v060()
install_v061()
install_v063()

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
