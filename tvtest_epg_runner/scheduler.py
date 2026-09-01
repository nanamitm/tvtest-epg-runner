"""Decide when to capture, and run one round across the configured drivers."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from .capture import CaptureRequest, CaptureResult, CaptureRunner
from .edcb import EdcbClient, EdcbUnavailable, free_until
from .util import format_duration

logger = logging.getLogger(__name__)


def next_run_after(config, moment):
    """The next scheduled moment strictly after ``moment``."""
    if config.every:
        return moment + timedelta(seconds=config.every)

    candidates = []
    for entry in config.times:
        for day in (0, 1):
            candidate = datetime.combine(moment.date() + timedelta(days=day), entry)
            if candidate > moment:
                candidates.append(candidate)
    return min(candidates)


class Scheduler:
    """Runs capture rounds on a schedule, and on demand from the tray menu."""

    def __init__(self, config, on_change=None, notifier=None):
        self.config = config
        self.on_change = on_change or (lambda: None)
        self.notifier = notifier
        self.runner = CaptureRunner(poll=config.edcb.poll, state_path=config.state_file)
        self.edcb = EdcbClient(
            config.edcb.url,
            timeout=config.edcb.timeout,
            default_start_margin=config.edcb.default_start_margin,
            default_end_margin=config.edcb.default_end_margin,
        )

        self.state = "起動中"
        self.busy = False
        self.current_driver = ""
        self.last_round = []
        self.last_finished = None
        self.next_run = None

        self._wake = threading.Event()
        self._quit = threading.Event()
        self._requests = []
        self._adoptable = None
        self._lock = threading.Lock()
        self._thread = None

    # -- control ---------------------------------------------------------

    def start(self):
        self.next_run = next_run_after(self.config, datetime.now())
        self._adoptable = self.runner.find_adoptable()
        if self.config.run_at_start and self._adoptable is None:
            self.request_round()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._quit.set()
        self.runner.request_cancel()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def request_round(self, drivers=None):
        """Ask for a capture round now.  ``drivers`` limits it to some names."""
        with self._lock:
            self._requests.append(drivers)
        self._wake.set()

    def cancel_current(self):
        self.runner.request_cancel()

    @property
    def running(self):
        """True while a round is in progress.

        This covers the whole round, not just the time TVTest is up: a round
        also spends time asking EDCB, and between two drivers.
        """
        return self.busy

    @property
    def capturing(self):
        return self.runner.running

    # -- main loop -------------------------------------------------------

    def _loop(self):
        self._set_state("待機中")
        self.adopt_pending()
        while not self._quit.is_set():
            timeout = max(1.0, (self.next_run - datetime.now()).total_seconds())
            self._wake.wait(timeout=min(timeout, 60))
            self._wake.clear()
            if self._quit.is_set():
                break

            while not self._quit.is_set():
                with self._lock:
                    if not self._requests:
                        break
                    requested = self._requests.pop(0)
                self._run_round(requested, scheduled=False)

            if not self._quit.is_set() and datetime.now() >= self.next_run:
                self.next_run = next_run_after(self.config, datetime.now())
                self._run_round(None, scheduled=True)

    def adopt_pending(self):
        """Take over a capture an earlier runner left running, if any.

        Killing the runner does not stop TVTest, so without this the capture
        would hold its tuner until its own time limit, unwatched and with
        nobody to report what it did.
        """
        adoptable = self._adoptable or self.runner.find_adoptable()
        self._adoptable = None
        if adoptable is None:
            return None

        driver = adoptable[1].driver
        self.busy = True
        self.current_driver = driver
        self._set_state(f"取得中: {driver}")
        try:
            result = self.runner.adopt(
                adoptable, watchdog=lambda elapsed: self._watchdog(driver, elapsed))
        finally:
            self.busy = False
            self.current_driver = ""

        self.last_round = [result]
        self.last_finished = datetime.now()
        self._set_state("待機中")
        self.notify([result])
        return result

    def _run_round(self, only, scheduled):
        drivers = self.config.enabled_drivers
        if only:
            wanted = set(only)
            drivers = [driver for driver in drivers if driver.name in wanted]
        if not drivers:
            return

        logger.info(
            "番組表の取得を開始します。(%s / 対象 %d ドライバ)",
            "定期実行" if scheduled else "手動実行", len(drivers),
        )
        results = []
        self.busy = True
        try:
            for driver in drivers:
                if self._quit.is_set():
                    break
                self.current_driver = driver.name
                self._set_state(f"取得中: {driver.name}")
                results.append(self.run_driver(driver))
                self.current_driver = ""
        finally:
            self.busy = False

        self.last_round = results
        self.last_finished = datetime.now()
        self._set_state("待機中")
        self.notify(results)

    def run_driver(self, driver):
        started = datetime.now()
        window, blocker = self.free_window(driver.name, started)

        if window is not None:
            if window < driver.min_window:
                reason = (
                    f"EDCB の予約まで {format_duration(window)} しかありません"
                    if blocker is not None else "EDCB のチューナーに空きがありません"
                )
                logger.info("%s をスキップします: %s", driver.name, reason)
                if blocker is not None:
                    logger.info("  次の予約: %s", blocker)
                return CaptureResult(
                    driver=driver.name, started=started, finished=datetime.now(),
                    exit_code=None, timeout=0, skipped=reason,
                )
            timeout = min(driver.timeout, window)
            if timeout < driver.timeout:
                logger.info(
                    "%s の制限時間を %s に縮めます。(次の予約まで %s)",
                    driver.name, format_duration(timeout), format_duration(window),
                )
        else:
            timeout = driver.timeout

        request = CaptureRequest(
            driver=driver.name, timeout=timeout,
            exe=self.config.exe, extra_args=self.config.extra_args,
        )
        return self.runner.run(
            request, watchdog=lambda elapsed: self._watchdog(driver.name, elapsed))

    def _watchdog(self, driver, elapsed):
        """Abort the capture once EDCB needs the tuner back."""
        window, blocker = self.free_window(driver, datetime.now())
        if window is None or window > 0:
            return None
        if blocker is not None:
            return f"EDCB の予約が近づきました ({blocker.title})"
        return "EDCB のチューナーに空きがなくなりました"

    def free_window(self, driver, moment):
        """Seconds this driver stays free, or None when EDCB cannot be asked."""
        try:
            reservations, counts = self.edcb.enum_tuner_reserve()
        except EdcbUnavailable as error:
            if self.config.edcb.required:
                logger.warning("EDCB に問い合わせできません: %s", error)
                return 0, None
            logger.debug("EDCB に問い合わせできません: %s", error)
            return None, None

        if driver not in counts:
            # EDCB does not use this BonDriver at all, so nothing can clash.
            logger.debug("%s は EDCB に登録されていません。", driver)
            return None, None

        return free_until(
            reservations, counts[driver], driver, moment, guard=self.config.edcb.guard)

    def _set_state(self, state):
        self.state = state
        self.on_change()

    def notify(self, results):
        if self.notifier is None:
            return
        try:
            self.notifier.send(results)
        except Exception:  # noqa: BLE001 - a status post must never break a round
            logger.exception("実行結果の通知に失敗しました。")
