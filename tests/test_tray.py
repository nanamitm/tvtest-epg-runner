import gc
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from tvtest_epg_runner.capture import EXIT_COMPLETED, EXIT_INCOMPLETE, CaptureResult
from tvtest_epg_runner.config import DriverConfig

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
    from tvtest_epg_runner.ui import app as app_module
except ImportError:  # CI はトレイを動かす環境を持たない
    app_module = None


@unittest.skipIf(app_module is None, 'PySide6 is not installed')
class TrayApplicationTest(unittest.TestCase):
    def setUp(self):
        self.scheduler = Mock(running=False, state='待機中', next_run=None,
                              last_round=[], last_finished=None)
        self.config = Mock(drivers=[DriverConfig('A.dll'), DriverConfig('B.dll')],
                           log_file='runner.log')
        self.server = Mock(running=False)
        self.tray = app_module.TrayApplication(self.scheduler, self.config, self.server)
        gc.collect()

    def labels(self, menu):
        return [action.text() for action in menu.actions() if not action.isSeparator()]

    def action(self, label):
        return next(action for action in self.tray.menu.actions()
                    if action.text() == label)

    # -- メニュー ----------------------------------------------------------

    def test_menu_keeps_every_action(self):
        self.assertEqual(self.labels(self.tray.menu), [
            '', '今すぐ取得', 'ドライバを指定して取得', '取得を中止', '前回の結果',
            '番組表を開く', '設定…', 'ログを開く', '終了'])

    def test_idle_menu(self):
        self.tray._update_menu()
        self.assertEqual(self.tray.status_action.text(), '待機中')
        self.assertTrue(self.tray.run_action.isEnabled())
        self.assertFalse(self.tray.cancel_action.isEnabled())
        self.assertTrue(self.tray.driver_menu.isEnabled())
        self.assertEqual(self.labels(self.tray.driver_menu), ['A.dll', 'B.dll'])
        self.assertEqual(self.labels(self.tray.results_menu), ['まだ実行していません'])
        self.assertFalse(self.tray.guide_action.isVisible())

    def test_status_line_shows_next_run(self):
        self.scheduler.next_run = datetime(2026, 10, 6, 4, 30)
        self.tray._update_menu()
        self.assertEqual(self.tray.status_action.text(), '待機中 (次回 10/06 04:30)')

    def test_busy_menu(self):
        self.scheduler.running = True
        self.scheduler.state = 'A.dll を取得中'
        self.tray._update_menu()
        self.assertEqual(self.tray.status_action.text(), 'A.dll を取得中')
        self.assertFalse(self.tray.run_action.isEnabled())
        self.assertTrue(self.tray.cancel_action.isEnabled())
        self.assertFalse(self.tray.driver_menu.isEnabled())

    def test_driver_entry_requests_that_driver(self):
        self.tray._update_menu()
        self.tray.driver_menu.actions()[1].trigger()
        self.scheduler.request_round.assert_called_once_with(['B.dll'])

    def test_menu_rebuild_does_not_pile_up_drivers(self):
        self.tray._update_menu()
        self.tray._update_menu()
        self.assertEqual(self.labels(self.tray.driver_menu), ['A.dll', 'B.dll'])

    def test_results_menu(self):
        started = datetime(2026, 10, 5, 4, 30)
        self.scheduler.last_finished = datetime(2026, 10, 5, 5, 8)
        self.scheduler.last_round = [
            CaptureResult('A.dll', started, started + timedelta(minutes=10),
                          EXIT_COMPLETED, 3000),
            CaptureResult('B.dll', started, started, None, 0, skipped='空きなし'),
        ]
        self.tray._update_menu()
        self.assertEqual(self.labels(self.tray.results_menu), [
            '10/05 05:08 実行', 'A.dll: 完了 (10分)', 'B.dll: スキップ: 空きなし'])

    def test_guide_follows_server(self):
        self.server.running = True
        self.tray._update_menu()
        self.assertTrue(self.tray.guide_action.isVisible())

    def test_actions_are_wired(self):
        with patch.object(self.tray, 'open_settings') as open_settings, \
                patch.object(self.tray, '_open') as open_file:
            self.action('今すぐ取得').trigger()
            self.action('取得を中止').trigger()
            self.action('ログを開く').trigger()
            self.tray._on_activated(QSystemTrayIcon.Trigger)
        self.scheduler.request_round.assert_called_once_with()
        self.scheduler.cancel_current.assert_called_once_with()
        open_file.assert_called_once_with('runner.log')
        open_settings.assert_called_once_with()

    # -- アイコン ----------------------------------------------------------

    def refreshed_icon(self):
        self.tray.tray = Mock()
        self.tray._refresh()
        icon = self.tray.tray.setIcon.call_args.args[0]
        return next(key for key, value in self.tray._icons.items() if value is icon)

    def test_icon_reflects_state(self):
        now = datetime.now()
        self.assertEqual(self.refreshed_icon(), 'idle')
        self.scheduler.last_round = [
            CaptureResult('A.dll', now, now, None, 0, skipped='空きなし')]
        self.assertEqual(self.refreshed_icon(), 'idle')
        self.scheduler.last_round = [CaptureResult('A.dll', now, now, EXIT_INCOMPLETE, 0)]
        self.assertEqual(self.refreshed_icon(), 'failed')
        self.scheduler.running = True
        self.assertEqual(self.refreshed_icon(), 'running')

    # -- 終了 --------------------------------------------------------------

    def quit_with(self, answer):
        self.tray.app = Mock()
        with patch.object(QMessageBox, 'question', return_value=answer) as question:
            self.action('終了').trigger()
        return question

    def test_quit_when_idle_needs_no_confirmation(self):
        question = self.quit_with(QMessageBox.No)
        question.assert_not_called()
        self.scheduler.stop.assert_called_once_with()
        self.tray.app.quit.assert_called_once_with()

    def test_quit_during_capture_can_be_declined(self):
        self.scheduler.running = True
        question = self.quit_with(QMessageBox.No)
        question.assert_called_once()
        self.scheduler.stop.assert_not_called()
        self.tray.app.quit.assert_not_called()

    def test_quit_during_capture_when_confirmed(self):
        self.scheduler.running = True
        self.quit_with(QMessageBox.Yes)
        self.scheduler.stop.assert_called_once_with()
        self.tray.app.quit.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
