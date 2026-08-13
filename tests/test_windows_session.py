from __future__ import annotations

import unittest

from assistant.windows_session import (
    ENDSESSION_LOGOFF,
    WM_ENDSESSION,
    WM_QUERYENDSESSION,
    WindowsSessionHook,
)


class Backend:
    def __init__(self):
        self.handler = None
        self.uninstall_calls = 0

    def install(self, handler):
        self.handler = handler

    def uninstall(self):
        self.uninstall_calls += 1


class WindowsSessionTests(unittest.TestCase):
    def test_query_acknowledges_and_end_session_requests_shutdown(self):
        backend = Backend()
        reasons = []
        hook = WindowsSessionHook(object(), reasons.append, backend=backend)
        self.assertTrue(hook.install())
        self.assertEqual(hook._dispatch(WM_QUERYENDSESSION), 1)
        hook._dispatch(WM_ENDSESSION, 1, 0)
        self.assertEqual(reasons, ["windows_shutdown"])
        hook._dispatch(WM_ENDSESSION, 1, ENDSESSION_LOGOFF)
        self.assertEqual(reasons[-1], "windows_logoff")
        hook.uninstall()
        self.assertEqual(backend.uninstall_calls, 1)


if __name__ == "__main__":
    unittest.main()
