"""The tray icon: what the runner is doing, and the buttons to steer it."""

from __future__ import annotations

import logging
import os

import pystray
from PIL import Image, ImageDraw

from .util import format_duration

logger = logging.getLogger(__name__)

IDLE = (0x3F, 0x8F, 0xD0)
RUNNING = (0x37, 0xA8, 0x5C)
FAILED = (0xC8, 0x4B, 0x3B)


def _icon_image(color):
    """A simple dish-and-dot mark, tinted by what the runner is doing."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color)
    draw.arc((14, 14, size - 14, size - 14), start=200, end=340, fill="white", width=5)
    draw.ellipse((28, 34, 36, 42), fill="white")
    return image


class TrayApplication:
    def __init__(self, scheduler, config):
        self.scheduler = scheduler
        self.config = config
        self._images = {
            "idle": _icon_image(IDLE),
            "running": _icon_image(RUNNING),
            "failed": _icon_image(FAILED),
        }
        self.icon = pystray.Icon(
            "tvtest-epg-runner",
            icon=self._images["idle"],
            title="TVTest EPG Runner",
            menu=self._build_menu(),
        )

    def run(self):
        self.scheduler.on_change = self.refresh
        self.refresh()
        self.icon.run()

    # -- menu ------------------------------------------------------------

    def _build_menu(self):
        driver_items = [
            pystray.MenuItem(
                driver.name,
                self._make_run_action(driver.name),
                enabled=lambda _item: not self.scheduler.running,
            )
            for driver in self.config.drivers
        ]

        return pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "今すぐ取得",
                self._run_all,
                enabled=lambda _item: not self.scheduler.running,
            ),
            pystray.MenuItem("ドライバを指定して取得", pystray.Menu(*driver_items)),
            pystray.MenuItem(
                "取得を中止",
                self._cancel,
                enabled=lambda _item: self.scheduler.running,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("前回の結果", pystray.Menu(self._last_results)),
            pystray.MenuItem("ログを開く", self._open_log),
            pystray.MenuItem("設定を開く", self._open_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", self._quit),
        )

    def _status_text(self, _item=None):
        scheduler = self.scheduler
        if scheduler.running and scheduler.current_driver:
            return f"取得中: {scheduler.current_driver}"
        if scheduler.next_run is not None:
            return f"待機中 (次回 {scheduler.next_run:%m/%d %H:%M})"
        return scheduler.state

    def _last_results(self):
        results = self.scheduler.last_round
        if not results:
            yield pystray.MenuItem("まだ実行していません", None, enabled=False)
            return
        finished = self.scheduler.last_finished
        if finished is not None:
            yield pystray.MenuItem(f"{finished:%m/%d %H:%M} 実行", None, enabled=False)
        for result in results:
            label = f"{result.driver}: {result.text}"
            if not result.skipped:
                label += f" ({format_duration(result.elapsed)})"
            yield pystray.MenuItem(label, None, enabled=False)

    def _make_run_action(self, driver):
        def action(_icon=None, _item=None):
            self.scheduler.request_round([driver])
        return action

    def _run_all(self, _icon=None, _item=None):
        self.scheduler.request_round()

    def _cancel(self, _icon=None, _item=None):
        self.scheduler.cancel_current()

    def _open_log(self, _icon=None, _item=None):
        self._open(self.config.log_file)

    def _open_config(self, _icon=None, _item=None):
        self._open(self.config.path)

    def _open(self, path):
        try:
            os.startfile(path)  # noqa: S606 - opening the user's own file
        except OSError as error:
            logger.warning("%s を開けません: %s", path, error)

    def _quit(self, _icon=None, _item=None):
        logger.info("終了します。")
        self.scheduler.stop()
        self.icon.stop()

    # -- state -----------------------------------------------------------

    def refresh(self):
        scheduler = self.scheduler
        if scheduler.running:
            key = "running"
        elif any(
            not result.skipped and not result.ok for result in scheduler.last_round
        ):
            key = "failed"
        else:
            key = "idle"

        self.icon.icon = self._images[key]
        self.icon.title = f"TVTest EPG Runner - {self._status_text()}"
        self.icon.update_menu()
