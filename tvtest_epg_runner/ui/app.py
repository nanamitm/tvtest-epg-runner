"""The tray application.

The scheduler runs on its own thread and reports state changes through a Qt
signal, which is how those changes reach the GUI thread safely.
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from ..util import format_duration
from .icons import FAILED, IDLE, RUNNING, make_icon
from .settings import SettingsDialog

logger = logging.getLogger(__name__)


class SchedulerBridge(QObject):
    """Carries the scheduler thread's notifications into the GUI thread."""

    changed = Signal()


class TrayApplication:
    def __init__(self, scheduler, config):
        self.scheduler = scheduler
        self.config = config
        self.dialog = None

        self.app = QApplication.instance() or QApplication([])
        self.app.setApplicationName("TVTest EPG Runner")
        self.app.setQuitOnLastWindowClosed(False)

        self._icons = {
            "idle": make_icon(IDLE),
            "running": make_icon(RUNNING),
            "failed": make_icon(FAILED),
        }

        self.tray = QSystemTrayIcon(self._icons["idle"])
        self.tray.setToolTip("TVTest EPG Runner")
        self.tray.activated.connect(self._on_activated)

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._update_menu)
        self._build_menu()
        self.tray.setContextMenu(self.menu)

        self.bridge = SchedulerBridge()
        self.bridge.changed.connect(self._refresh, Qt.QueuedConnection)
        scheduler.on_change = self.bridge.changed.emit

    def run(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(
                None, "TVTest EPG Runner", "通知領域を利用できません。")
            return 1

        self.tray.show()
        self.scheduler.start()
        self._refresh()
        return self.app.exec()

    # -- メニュー ----------------------------------------------------------

    def _build_menu(self):
        self.status_action = QAction("")
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()

        self.run_action = QAction("今すぐ取得")
        self.run_action.triggered.connect(lambda: self.scheduler.request_round())
        self.menu.addAction(self.run_action)

        self.driver_menu = self.menu.addMenu("ドライバを指定して取得")
        self.cancel_action = QAction("取得を中止")
        self.cancel_action.triggered.connect(self.scheduler.cancel_current)
        self.menu.addAction(self.cancel_action)

        self.menu.addSeparator()
        self.results_menu = self.menu.addMenu("前回の結果")

        settings_action = QAction("設定…")
        settings_action.triggered.connect(self.open_settings)
        self.menu.addAction(settings_action)

        log_action = QAction("ログを開く")
        log_action.triggered.connect(lambda: self._open(self.config.log_file))
        self.menu.addAction(log_action)

        self.menu.addSeparator()
        quit_action = QAction("終了")
        quit_action.triggered.connect(self._quit)
        self.menu.addAction(quit_action)

    def _update_menu(self):
        busy = self.scheduler.running
        self.status_action.setText(self._status_text())
        self.run_action.setEnabled(not busy)
        self.cancel_action.setEnabled(busy)

        self.driver_menu.clear()
        self.driver_menu.setEnabled(not busy)
        for driver in self.config.drivers:
            action = self.driver_menu.addAction(driver.name)
            action.triggered.connect(
                lambda _checked=False, name=driver.name:
                self.scheduler.request_round([name]))

        self.results_menu.clear()
        results = self.scheduler.last_round
        if not results:
            self.results_menu.addAction("まだ実行していません").setEnabled(False)
            return
        if self.scheduler.last_finished is not None:
            header = self.results_menu.addAction(
                f"{self.scheduler.last_finished:%m/%d %H:%M} 実行")
            header.setEnabled(False)
        for result in results:
            label = f"{result.driver}: {result.text}"
            if not result.skipped:
                label += f" ({format_duration(result.elapsed)})"
            self.results_menu.addAction(label).setEnabled(False)

    def _status_text(self):
        scheduler = self.scheduler
        if scheduler.running:
            return scheduler.state
        if scheduler.next_run is not None:
            return f"待機中 (次回 {scheduler.next_run:%m/%d %H:%M})"
        return scheduler.state

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_settings()

    # -- 設定 --------------------------------------------------------------

    def open_settings(self):
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.raise_()
            self.dialog.activateWindow()
            return

        self.dialog = SettingsDialog(self.config)
        if self.dialog.exec() == SettingsDialog.Accepted and self.dialog.saved_config:
            self.config = self.dialog.saved_config
            self.scheduler.reconfigure(self.config)
            self.tray.showMessage(
                "TVTest EPG Runner", "設定を保存しました。",
                QSystemTrayIcon.Information, 3000)
            self._refresh()
        self.dialog = None

    # -- 状態 --------------------------------------------------------------

    def _refresh(self):
        scheduler = self.scheduler
        if scheduler.running:
            key = "running"
        elif any(not r.skipped and not r.ok for r in scheduler.last_round):
            key = "failed"
        else:
            key = "idle"

        self.tray.setIcon(self._icons[key])
        self.tray.setToolTip(f"TVTest EPG Runner - {self._status_text()}")

    def _open(self, path):
        if not os.path.exists(path):
            QMessageBox.information(
                self.dialog, "TVTest EPG Runner", f"まだありません: {path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))

    def _quit(self):
        logger.info("終了します。")
        self.tray.hide()
        self.scheduler.stop()
        self.app.quit()
