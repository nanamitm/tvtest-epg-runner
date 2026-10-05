"""Small helpers shared by the runner."""

from __future__ import annotations

import re
import subprocess
from datetime import timedelta


def format_arguments(arguments):
    """Render an argument list using Windows command-line quoting."""
    return subprocess.list2cmdline(arguments)


def parse_arguments(text):
    """Read Windows quoting without treating path backslashes as escapes."""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR,
                                         ctypes.POINTER(ctypes.c_int))
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    # argv[0] follows different quoting rules; prepend a disposable name.
    argv = shell32.CommandLineToArgvW('runner ' + text, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(1, count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))

_DURATION_RE = re.compile(r"(\d+)\s*([hms]?)", re.IGNORECASE)
_DURATION_FULL_RE = re.compile(r"(?:\s*\d+\s*[hms]?\s*)+", re.IGNORECASE)


def parse_duration(value, default=None):
    """Parse "40m", "1h30m", "90s" or a plain number of seconds.

    The format matches TVTest's own /epgcapturetimeout and /recduration.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    if not text:
        return default

    if not _DURATION_FULL_RE.fullmatch(text):
        raise ValueError(f"時間の指定を解釈できません: {value!r}")

    total = 0
    matched = False
    for number, unit in _DURATION_RE.findall(text):
        matched = True
        amount = int(number)
        if unit in ("h", "H"):
            total += amount * 3600
        elif unit in ("m", "M"):
            total += amount * 60
        else:
            total += amount
    if not matched:
        raise ValueError(f"時間の指定を解釈できません: {value!r}")
    return total


def format_duration(seconds):
    """Render a number of seconds the way the log and the tray menu want it."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}分" if rest == 0 else f"{minutes}分{rest}秒"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}時間" if minutes == 0 else f"{hours}時間{minutes}分"


def format_timedelta(delta: timedelta):
    return format_duration(max(0, int(delta.total_seconds())))
