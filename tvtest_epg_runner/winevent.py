"""Signal TVTest's EPG capture cancel events.

TVTest creates ``TVTest_EpgCaptureCancel`` and
``TVTest_EpgCaptureCancel_<pid>`` while a command line capture is running, and
aborts the capture when either becomes signaled.  The events live in the
session local namespace, so this only works from the session that started
TVTest.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

EVENT_MODIFY_STATE = 0x0002
ERROR_FILE_NOT_FOUND = 2

CANCEL_EVENT_NAME = "TVTest_EpgCaptureCancel"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateMutexW.restype = wintypes.HANDLE


def cancel_event_name(pid=None):
    if pid is None:
        return CANCEL_EVENT_NAME
    return f"{CANCEL_EVENT_NAME}_{pid}"


def signal_cancel(pid=None):
    """Signal the cancel event.  Returns False when it does not exist yet.

    TVTest creates the events when the capture actually begins, which is a few
    seconds after launch, so a False here usually means "not capturing yet".
    """
    name = cancel_event_name(pid)
    handle = _kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:
            return False
        raise ctypes.WinError(error)
    try:
        if not _kernel32.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel32.CloseHandle(handle)
    return True


def acquire_single_instance(name="TVTestEpgRunner_SingleInstance"):
    """Take a named mutex.  Returns the handle, or None if already held.

    The handle is kept alive for the lifetime of the process; Windows releases
    it when the process ends.
    """
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return handle


_user32 = ctypes.WinDLL("user32", use_last_error=True)

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = (_ENUM_PROC, wintypes.LPARAM)
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetClassNameW.restype = ctypes.c_int
_user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_user32.PostMessageW.restype = wintypes.BOOL

WM_CLOSE = 0x0010
MAIN_WINDOW_CLASS = "TVTest Window"


def post_close(pid):
    """Ask TVTest's main window to close.  Returns False when not found.

    Only the "TVTest Window" class closes the application; the panel frame and
    the other top level windows ignore WM_CLOSE.
    """
    found = []

    def callback(hwnd, _lparam):
        window_pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            buffer = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, buffer, len(buffer))
            if buffer.value == MAIN_WINDOW_CLASS:
                found.append(hwnd)
                return False
        return True

    _user32.EnumWindows(_ENUM_PROC(callback), 0)
    if not found:
        return False
    return bool(_user32.PostMessageW(found[0], WM_CLOSE, 0, 0))
