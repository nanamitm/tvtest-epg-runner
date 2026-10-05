import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from tvtest_epg_runner.config import ServerConfig
from tvtest_epg_runner.syncserver import SyncServer
from tvtest_epg_runner import capture
from tvtest_epg_runner.config import Config, DriverConfig
from tvtest_epg_runner.scheduler import Scheduler
from tvtest_epg_runner.edcb import Reservation
from tvtest_epg_runner.channels import ChannelGroup, Service
from tvtest_epg_runner.util import format_arguments, parse_arguments


class ArgumentEditingTest(unittest.TestCase):
    def test_windows_arguments_round_trip(self):
        arguments = ['/log', '/logfile', 'C:\\TVTest Data\\capture.log',
                     'C:\\folder with spaces\\', '', 'a"quoted"value',
                     '日本語 パス', 'C:\\plain\\file.txt']
        self.assertEqual(parse_arguments(format_arguments(arguments)), arguments)

    def test_quoted_path_and_empty_editor(self):
        self.assertEqual(parse_arguments('/logfile "C:\\TVTest Data\\capture.log"'),
                         ['/logfile', 'C:\\TVTest Data\\capture.log'])
        self.assertEqual(parse_arguments(''), [])


class SchedulerRegressionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(exe='unused', every=3600,
                             state_dir=self.directory.name)
        with patch('tvtest_epg_runner.scheduler.supported_options',
                   return_value=set(capture.REQUIRED_OPTIONS)):
            self.scheduler = Scheduler(self.config)

    def test_adopted_captures_release_tuner_for_recording(self):
        now = datetime.now()
        reservation = Reservation('1', 'recording', now - timedelta(seconds=1),
                                  now + timedelta(hours=1), '0', 'D.dll')
        self.scheduler._reservations = lambda: ([reservation], {'D.dll': 2})
        reasons = []
        def adopt(entry, watchdog):
            reasons.append(watchdog(0))
            return capture.CaptureResult('D.dll', now, now, 0, 2400)
        self.scheduler._adoptable = [
            (Mock(state_path=f'capture-D.dll-{index}.json', adopt=adopt),
             (None, capture.CaptureRequest('D.dll', 2400, 'unused'), now))
            for index in (1, 2)
        ]
        self.scheduler.adopt_pending()
        self.assertEqual(sum(reason is not None for reason in reasons), 1)

    def test_surviving_second_capture_keeps_its_position(self):
        now = datetime.now()
        runner = Mock(state_path='capture-D.dll-2.json')
        def adopt(entry, watchdog):
            watchdog(0)
            return capture.CaptureResult('D.dll', now, now, 0, 2400)
        runner.adopt = adopt
        self.scheduler._adoptable = [(runner, (None,
            capture.CaptureRequest('D.dll', 2400, 'unused'), now))]
        with patch.object(self.scheduler, '_watchdog', return_value=None) as watchdog:
            self.scheduler.adopt_pending()
        watchdog.assert_called_once_with('D.dll', 2, 0)

    def test_older_tvtest_still_launches_fallback_capture(self):
        self.scheduler.missing_options = list(capture.REQUIRED_OPTIONS)
        self.scheduler.free_window = lambda *args, **kwargs: (2400, None)
        jobs, skipped = self.scheduler._plan(DriverConfig('D.dll', instances=2))
        self.assertIsNone(skipped)
        self.assertEqual(len(jobs), 2)
        with patch.object(jobs[0].runner, 'run', return_value=Mock(ok=False, report=[])) as run:
            self.scheduler._run_job(jobs[0])
        self.assertEqual(run.call_args.args[0].channels, '')

    def test_channel_limit_survives_disabled_priority(self):
        self.config.priority.enabled = False
        self.scheduler.free_window = lambda *args, **kwargs: (2400, None)
        jobs, skipped = self.scheduler._plan(DriverConfig('D.dll', channels='2:15'))
        self.assertIsNone(skipped)
        with patch.object(jobs[0].runner, 'run', return_value=Mock(ok=False, report=[])) as run:
            self.scheduler._run_job(jobs[0])
        self.assertEqual(run.call_args.args[0].channels, '2:15')

    def test_invalid_channel_limit_falls_back_to_all_channels(self):
        self.config.priority.enabled = False
        self.scheduler.free_window = lambda *args, **kwargs: (2400, None)
        jobs, skipped = self.scheduler._plan(DriverConfig('D.dll', channels='2:x'))
        self.assertIsNone(skipped)
        with (patch.object(jobs[0].runner, 'run', return_value=Mock(ok=False, report=[])) as run,
              self.assertLogs('tvtest_epg_runner.scheduler', 'WARNING')):
            self.scheduler._run_job(jobs[0])
        self.assertEqual(run.call_args.args[0].channels, '')

    def test_freshness_is_scoped_to_each_driver(self):
        self.config.drivers = [DriverConfig('A.dll'), DriverConfig('B.dll')]
        a = ChannelGroup(0, 0, [Service('A', 1, 1, 1)])
        b = ChannelGroup(0, 0, [Service('B', 2, 2, 2)])
        other = ChannelGroup(0, 1, [Service('other', 3, 3, 3)])
        self.scheduler._groups_for = lambda driver: [a] if driver.name == 'A.dll' else [b]
        now = datetime.now()
        old = now - timedelta(days=2)
        self.scheduler.notifier = Mock()
        self.scheduler.notifier.fetch_service_times.return_value = {
            (1, 1, 1): old, (2, 2, 2): now}
        freshness = self.scheduler._addon_freshness()
        self.assertEqual(freshness[('A.dll', '0:0')], old)
        self.assertEqual(freshness[('B.dll', '0:0')], now)
        self.scheduler.history.mark_attempted('A.dll', [other], now - timedelta(days=1))
        self.assertEqual(self.scheduler.history.order('A.dll', [a, other], freshness),
                         [a, other])


class ServerStartupTest(unittest.TestCase):
    def test_second_port_failure_closes_unstarted_server(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Mock()
            api = Mock()
            module.make_server.side_effect = [api, OSError('port occupied')]
            server = SyncServer(ServerConfig(enabled=True, data_dir=directory))
            with patch('tvtest_epg_runner.syncserver.load_module', return_value=module):
                self.assertFalse(server.start())
            api.shutdown.assert_not_called()
            api.server_close.assert_called_once()
            self.assertFalse(server.running)

    def test_close_error_during_failed_startup_is_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Mock()
            api = Mock()
            api.server_close.side_effect = OSError('close failed')
            module.make_server.side_effect = [api, OSError('port occupied')]
            server = SyncServer(ServerConfig(enabled=True, data_dir=directory))
            with patch('tvtest_epg_runner.syncserver.load_module', return_value=module):
                self.assertFalse(server.start())
            api.shutdown.assert_not_called()
            api.server_close.assert_called_once()
            self.assertFalse(server.running)
