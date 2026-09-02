"""Start the runner when the user logs on.

A logon entry, not a service: the tray icon needs a desktop, and the event
that cancels a capture lives in the session namespace, so the runner has to
be in the same session as the TVTest it starts.
"""

from __future__ import annotations

import logging
import os
import sys
import winreg

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "TVTestEpgRunner"

# パッケージの1つ上、リポジトリのルートに置いてある入口
LAUNCHER = "run.pyw"


def launcher_path():
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(package_dir), LAUNCHER)


def interpreter_path():
    """pythonw.exe を選ぶ (コンソールを出さないため)"""
    executable = sys.executable or ""
    directory, name = os.path.split(executable)
    if name.lower() == "python.exe":
        windowed = os.path.join(directory, "pythonw.exe")
        if os.path.isfile(windowed):
            return windowed
    return executable


def command():
    return f'"{interpreter_path()}" "{launcher_path()}"'


def is_enabled():
    """True when the logon entry points at this copy of the runner."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return False
    except OSError as error:
        logger.warning("自動起動の設定を読めません: %s", error)
        return False

    # 別の場所のランナーが登録されている場合も「有効」とは見なす
    return launcher_path().lower() in str(value).lower()


def enable():
    if not os.path.isfile(launcher_path()):
        raise OSError(f"起動用のファイルがありません: {launcher_path()}")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
    logger.info("ログオン時に起動するようにしました。(%s)", command())


def disable():
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return
    logger.info("ログオン時の起動をやめました。")


def apply(enabled):
    """Make the logon entry match ``enabled``; returns True when it changed."""
    if enabled == is_enabled():
        return False
    if enabled:
        enable()
    else:
        disable()
    return True
