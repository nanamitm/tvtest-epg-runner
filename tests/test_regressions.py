import tempfile
import unittest
from unittest.mock import Mock, patch

from tvtest_epg_runner.config import ServerConfig
from tvtest_epg_runner.syncserver import SyncServer


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
