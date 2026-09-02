"""Run the EPG sync server in this process, for a LAN without Home Assistant.

The add-on's server is plain standard library Python, so the same code that
runs in Home Assistant can serve from here: TVTest instances point at the API
port, and the web guide answers on the other one.  It is carried as a
submodule rather than copied, so there is one implementation to fix.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

# 配布用にひとつにまとめた場合、サーバのソースは展開先に置かれる
BUNDLED_NAME = "epgsync"


def _app_dir():
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return os.path.join(bundle, BUNDLED_NAME)
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "thirdparty", "home-assistant-addons", "tvtest_epg_sync", "app",
    )


APP_DIR = _app_dir()


class SyncUnavailable(Exception):
    """The add-on's sources are not where the submodule should have put them."""


def load_module():
    """Import the add-on's server module from the submodule."""
    if not os.path.isfile(os.path.join(APP_DIR, "server.py")):
        raise SyncUnavailable(
            f"EPG 共有サーバのソースがありません: {APP_DIR}\n"
            "git submodule update --init を実行してください。")

    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)

    import server  # noqa: PLC0415 - サブモジュールを読み込んだ後でしか import できない

    return server


class SyncServer:
    """The sync server, started and stopped with the tray application."""

    def __init__(self, config):
        self.config = config
        self._servers = []
        self._threads = []
        self._stop = threading.Event()
        self._module = None

    @property
    def running(self):
        return bool(self._servers)

    @property
    def api_url(self):
        return f"http://127.0.0.1:{self.config.api_port}"

    @property
    def ui_url(self):
        if self.config.ui_port <= 0:
            return self.api_url
        return f"http://127.0.0.1:{self.config.ui_port}"

    # -- 起動と停止 --------------------------------------------------------

    def start(self):
        if self.running or not self.config.enabled:
            return False

        try:
            module = load_module()
        except SyncUnavailable as error:
            logger.error("%s", error)
            return False

        data_dir = self.config.data_dir
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as error:
            logger.error("EPG の保管先を作れません: %s (%s)", data_dir, error)
            return False

        servers = []
        try:
            store = module.Store(data_dir)
            store.purge_older_than(self.config.retention_days)
            context = module.Context(store, module.EventBus(), self.config.token)

            # TVTest 用はトークンを要求し、ブラウザ用は要求しない
            servers.append(module.make_server(self.config.api_port, context, True))
            if self.config.ui_port > 0:
                servers.append(
                    module.make_server(self.config.ui_port, context, False))
        except OSError as error:
            logger.error("EPG 共有サーバを開始できません: %s", error)
            self._close(servers)
            return False

        self._module = module
        self._servers = servers
        self._stop.clear()

        for server in servers:
            thread = threading.Thread(
                target=server.serve_forever, name="epgsync", daemon=True)
            thread.start()
            self._threads.append(thread)

        purge = threading.Thread(
            target=module.purge_loop,
            args=(store, self.config.retention_days, self._stop),
            name="epgsync-purge", daemon=True)
        purge.start()
        self._threads.append(purge)

        logger.info(
            "EPG 共有サーバを開始しました。(TVTest 用 %d 番, 番組表 %s / 保管先 %s)",
            self.config.api_port,
            f"{self.config.ui_port} 番" if self.config.ui_port > 0 else "なし",
            data_dir,
        )
        if not self.config.token:
            logger.warning(
                "トークンが未設定です。ポート %d に届く相手は誰でも EPG を読み書きできます。",
                self.config.api_port)
        return True

    def stop(self):
        if not self.running:
            return
        self._stop.set()
        self._close(self._servers)
        for thread in self._threads:
            thread.join(timeout=10)
        self._servers = []
        self._threads = []
        logger.info("EPG 共有サーバを停止しました。")

    def reconfigure(self, config):
        """Apply saved settings, restarting only when something changed."""
        if config == self.config and self.running == config.enabled:
            return
        self.stop()
        self.config = config
        self.start()

    @staticmethod
    def _close(servers):
        for server in servers:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001 - 停止処理で落ちても意味がない
                logger.debug("サーバの停止に失敗しました。", exc_info=True)
