from __future__ import annotations

"""Win32 session-end adapter for Nova's Tk window."""

import os

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
ENDSESSION_CLOSEAPP = 0x00000001
ENDSESSION_LOGOFF = 0x80000000
GWLP_WNDPROC = -4


class WindowsSessionHook:
    def __init__(self, root, callback, *, backend=None):
        self.root = root
        self.callback = callback
        self.backend = backend
        self.installed = False
        self._hwnd = 0
        self._previous = 0
        self._proc = None

    def _reason(self, flags: int) -> str:
        if flags & ENDSESSION_LOGOFF:
            return "windows_logoff"
        if flags & ENDSESSION_CLOSEAPP:
            return "windows_closeapp"
        return "windows_shutdown"

    def install(self) -> bool:
        if self.installed:
            return True
        if self.backend is not None:
            self.backend.install(self._dispatch)
            self.installed = True
            return True
        if os.name != "nt":
            return False

        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(self.root.winfo_id())
        wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        set_wndproc = user32.SetWindowLongPtrW
        set_wndproc.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        set_wndproc.restype = ctypes.c_void_p
        call_wndproc = user32.CallWindowProcW
        call_wndproc.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        call_wndproc.restype = ctypes.c_ssize_t

        @wndproc_type
        def proc(window, message, wparam, lparam):
            if message == WM_QUERYENDSESSION:
                return 1
            if message == WM_ENDSESSION and wparam:
                reason = self._reason(int(lparam))
                try:
                    self.root.after(0, lambda r=reason: self.callback(r))
                except Exception:
                    pass
            return call_wndproc(self._previous, window, message, wparam, lparam)

        self._proc = proc
        self._hwnd = hwnd
        previous = set_wndproc(hwnd, GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
        if not previous:
            self._proc = None
            self._hwnd = 0
            return False
        self._previous = previous
        self.installed = True
        return True

    def _dispatch(self, message: int, wparam: int = 0, lparam: int = 0):
        if message == WM_QUERYENDSESSION:
            return 1
        if message == WM_ENDSESSION and wparam:
            self.callback(self._reason(int(lparam)))
        return 0

    def uninstall(self) -> None:
        if not self.installed:
            return
        if self.backend is not None:
            self.backend.uninstall()
        elif os.name == "nt" and self._hwnd and self._previous:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetWindowLongPtrW(self._hwnd, GWLP_WNDPROC, self._previous)
        self.installed = False
        self._proc = None
        self._hwnd = 0
        self._previous = 0
