"""Is a stream an interactive console a human types into? (R3-11)

On Windows, isatty() is also True for NUL and other character devices, so a daemon started as
`omx daemon < NUL` (scheduled task, service wrapper) would claim a console. Here a stream counts
only when GetFileType(handle) == FILE_TYPE_CHAR AND GetConsoleMode(handle) succeeds (a real
console input). Pipes (e.g. Git Bash / mintty), files and NUL are not consoles. Elsewhere isatty().
"""
from __future__ import annotations

import os
from typing import Any

FILE_TYPE_CHAR = 0x0002


def is_console(stream: Any) -> bool:
    try:
        if stream is None or not stream.isatty():
            return False
        fd = stream.fileno()
    except (AttributeError, ValueError, OSError):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetFileType.argtypes, k32.GetFileType.restype = [wintypes.HANDLE], wintypes.DWORD
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        h = msvcrt.get_osfhandle(fd)
        mode = wintypes.DWORD()
        return k32.GetFileType(h) == FILE_TYPE_CHAR and bool(k32.GetConsoleMode(h, ctypes.byref(mode)))
    except Exception:            # cannot tell: not a console (human ops are then refused, fail closed)
        return False
