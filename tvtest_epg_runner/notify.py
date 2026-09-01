"""Report what a capture round did to the TVTest EPG Sync add-on."""

from __future__ import annotations

import logging
import platform
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

STATUS_PATH = "/api/runner-status"


class AddonNotifier:
    def __init__(self, config):
        self.url = config.url.rstrip("/")
        self.token = config.token
        self.name = config.name
        self.timeout = config.timeout
        self._warned = False

    @property
    def enabled(self):
        return bool(self.url)

    def send(self, results):
        if not self.enabled:
            return False

        payload = {
            "name": self.name,
            "host": platform.node(),
            "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
            "captures": [result.as_dict() for result in results],
        }
        headers = {"X-EPG-Source": self.name}
        if self.token:
            headers["X-EPG-Token"] = self.token

        try:
            response = requests.post(
                f"{self.url}{STATUS_PATH}", json=payload,
                headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as error:
            self._warn("実行結果を送信できません: %s", error)
            return False

        if response.status_code in (404, 405, 501):
            self._warn(
                "アドオンが %s に対応していません。アドオンを更新してください。(HTTP %s)",
                STATUS_PATH, response.status_code)
            return False
        if not response.ok:
            self._warn("実行結果の送信が拒否されました: HTTP %s", response.status_code)
            return False

        logger.info("実行結果を EPG 共有サーバへ送信しました。")
        self._warned = False
        return True

    def _warn(self, message, *args):
        # One warning per outage is enough; a nightly runner would otherwise
        # fill the log with the same line.
        if self._warned:
            logger.debug(message, *args)
        else:
            logger.warning(message, *args)
            self._warned = True
