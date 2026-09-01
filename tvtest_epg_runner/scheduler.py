"""Decide what to capture, when, and on how many tuners at once.

A round plans one or more jobs per driver: the least recently captured
channels first, as many as the free time before EDCB's next recording allows,
dealt across the tuners the driver can spare.  The jobs then run at the same
time, each in its own TVTest.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import channels as channel_module
from .capture import CaptureRequest, CaptureResult, CaptureRunner
from .edcb import EdcbClient, EdcbUnavailable, free_until
from .history import CaptureHistory
from .notify import AddonNotifier
from .util import format_duration

logger = logging.getLogger(__name__)

STATE_PREFIX = "capture-"


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


@dataclass
class Job:
    driver: object
    index: int          # 1 から数えた並列の位置
    parallel: int       # このドライバで同時に走る本数
    timeout: int
    runner: CaptureRunner
    groups: list = field(default_factory=list)

    @property
    def label(self):
        if self.parallel > 1:
            return f"{self.driver.name} ({self.index}/{self.parallel})"
        return self.driver.name


class Scheduler:
    def __init__(self, config, on_change=None):
        self.on_change = on_change or (lambda: None)
        self._apply(config)

        self.state = "起動中"
        self.busy = False
        self.last_round = []
        self.last_finished = None
        self.next_run = None

        self._wake = threading.Event()
        self._quit = threading.Event()
        self._requests = []
        self._active = {}
        self._runners = []
        self._adoptable = []
        self._lock = threading.Lock()
        self._thread = None

    def _apply(self, config):
        self.config = config
        self.edcb = EdcbClient(
            config.edcb.url,
            timeout=config.edcb.timeout,
            default_start_margin=config.edcb.default_start_margin,
            default_end_margin=config.edcb.default_end_margin,
        )
        self.history = CaptureHistory(config.history_file)
        self.notifier = AddonNotifier(config.addon) if config.addon.url else None

    def reconfigure(self, config):
        """Adopt settings saved from the dialog without restarting."""
        self._apply(config)
        self.next_run = next_run_after(config, datetime.now())
        logger.info("設定を読み込み直しました。(次回 %s)", f"{self.next_run:%m/%d %H:%M}")
        self._wake.set()
        self.on_change()

    # -- control ---------------------------------------------------------

    def start(self):
        self.next_run = next_run_after(self.config, datetime.now())
        self._adoptable = self._find_adoptable()
        if self.config.run_at_start and not self._adoptable:
            self.request_round()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._quit.set()
        self.cancel_current()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def request_round(self, drivers=None):
        with self._lock:
            self._requests.append(drivers)
        self._wake.set()

    def cancel_current(self):
        for runner in list(self._runners):
            runner.request_cancel()

    @property
    def running(self):
        """True while a round is in progress, not only while TVTest is up."""
        return self.busy

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
                self.run_round(requested, scheduled=False)

            if not self._quit.is_set() and datetime.now() >= self.next_run:
                self.next_run = next_run_after(self.config, datetime.now())
                self.run_round(None, scheduled=True)

    # -- one round -------------------------------------------------------

    def run_round(self, only=None, scheduled=False):
        drivers = self.config.enabled_drivers
        if only:
            wanted = set(only)
            drivers = [driver for driver in drivers if driver.name in wanted]
        if not drivers:
            return []

        logger.info(
            "番組表の取得を開始します。(%s / 対象 %d ドライバ)",
            "定期実行" if scheduled else "手動実行", len(drivers),
        )

        freshness = self._addon_freshness()
        jobs = []
        results = []
        for driver in drivers:
            planned, skipped = self._plan(driver, freshness)
            if skipped is not None:
                results.append(skipped)
            jobs.extend(planned)

        if jobs:
            results.extend(self._run_jobs(jobs))

        self.last_round = results
        self.last_finished = datetime.now()
        self._set_state("待機中")
        self.notify(results)
        return results

    def _run_jobs(self, jobs):
        self.busy = True
        self._runners = [job.runner for job in jobs]
        results = []
        try:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(self._run_job, job) for job in jobs]
                for future in futures:
                    results.append(future.result())
        finally:
            self.busy = False
            self._runners = []
            self._active.clear()
        return results

    def _run_job(self, job):
        self._activate(job.label)
        try:
            request = CaptureRequest(
                driver=job.driver.name,
                timeout=job.timeout,
                exe=self.config.exe,
                extra_args=self.config.extra_args,
                channels=channel_module.to_spec(job.groups) if job.groups else "",
                channel_count=len(job.groups),
                report_path=self._report_path(job.driver.name, job.index),
            )
            if job.groups:
                self.history.mark_attempted(job.driver.name, job.groups)
            result = job.runner.run(
                request,
                watchdog=lambda elapsed: self._watchdog(
                    job.driver.name, job.index, elapsed),
            )
            self._record(job, result)
            return result
        finally:
            self._deactivate(job.label)

    def _record(self, job, result):
        """Fold what happened back into the history."""
        self.history.apply_report(job.driver.name, result.report)
        if result.ok and job.groups:
            # 完了したなら、TVTest が1回にまとめたチャンネルも回り終えている
            self.history.mark_captured(job.driver.name, job.groups, result.finished)

    # -- planning --------------------------------------------------------

    def _plan(self, driver, freshness=None):
        """The jobs to run for one driver, or a result saying why it is skipped."""
        started = datetime.now()
        parallel = max(1, driver.instances)
        window, blocker = None, None

        # 確保できる本数まで下げながら、空き時間を調べる
        while parallel > 0:
            window, blocker = self.free_window(driver.name, started, needed=parallel)
            if window is None or window >= driver.min_window:
                break
            parallel -= 1

        if parallel == 0:
            reason = (
                f"EDCB の予約まで {format_duration(window or 0)} しかありません"
                if blocker is not None else "EDCB のチューナーに空きがありません"
            )
            logger.info("%s をスキップします: %s", driver.name, reason)
            if blocker is not None:
                logger.info("  次の予約: %s", blocker)
            return [], CaptureResult(
                driver=driver.name, started=started, finished=datetime.now(),
                exit_code=None, timeout=0, skipped=reason,
            )

        timeout = driver.timeout if window is None else min(driver.timeout, window)
        if window is not None and timeout < driver.timeout:
            logger.info(
                "%s の制限時間を %s に縮めます。(次の予約まで %s)",
                driver.name, format_duration(timeout), format_duration(window),
            )

        buckets = self._share_out(driver, parallel, timeout, freshness)
        jobs = []
        for index, groups in enumerate(buckets, start=1):
            if not groups and self.config.priority.enabled:
                continue
            jobs.append(Job(
                driver=driver, index=index, parallel=len(buckets), timeout=timeout,
                runner=CaptureRunner(
                    poll=self.config.edcb.poll,
                    state_path=self._state_path(driver.name, index),
                ),
                groups=groups,
            ))
        return jobs, None

    def _share_out(self, driver, parallel, timeout, freshness):
        """Pick the channels for this round and deal them across the tuners."""
        if not self.config.priority.enabled:
            # 優先順を使わないなら、対象の絞り込みは TVTest 側に任せる
            return [[] for _ in range(parallel)]

        groups = self._groups_for(driver)
        if not groups:
            return [[] for _ in range(parallel)]

        ordered = self.history.order(driver.name, groups, freshness)

        # 1本あたりの持ち時間から、今回いくつ回せるかを見積もる
        budget = max(0, timeout - self.config.priority.reserve) * parallel
        chosen = []
        spent = 0
        for group in ordered:
            estimate = self.history.estimate_seconds(driver.name, group)
            if chosen and spent + estimate > budget:
                break
            chosen.append(group)
            spent += estimate

        captured, total = self.history.summary(driver.name, groups)
        logger.info(
            "%s: %d/%d チャンネルを取得します。(取得済み %d/%d / 1本あたり約 %s)",
            driver.name, len(chosen), len(groups), captured, total,
            format_duration(spent // max(1, parallel)),
        )
        if chosen:
            logger.debug(
                "  対象: %s",
                ", ".join(f"{g.key}({g.name})" for g in chosen[:12])
                + (" …" if len(chosen) > 12 else ""),
            )

        buckets = [[] for _ in range(parallel)]
        for position, group in enumerate(chosen):
            buckets[position % parallel].append(group)
        return buckets

    def _groups_for(self, driver):
        path = channel_module.channel_file_for(self.config.exe, driver.name)
        groups = channel_module.load_groups(path)
        if not groups:
            return []
        try:
            return channel_module.select(groups, driver.channels)
        except ValueError as error:
            logger.warning("%s のチャンネル指定が不正です: %s", driver.name, error)
            return groups

    def _addon_freshness(self):
        """When each channel group was last refreshed by anyone on the LAN."""
        if not (self.config.priority.enabled and self.config.priority.use_addon):
            return {}
        if self.notifier is None:
            return {}

        services = self.notifier.fetch_service_times()
        if not services:
            return {}

        freshness = {}
        for driver in self.config.enabled_drivers:
            for group in self._groups_for(driver):
                times = [
                    services[service.key]
                    for service in group.services if service.key in services
                ]
                if times:
                    # まとまりの中で一番古いサービスに合わせる
                    freshness[group.key] = min(times)
        return freshness

    # -- EDCB ------------------------------------------------------------

    def free_window(self, driver, moment, needed=1):
        """Seconds this driver keeps ``needed`` tuners free, None if unknown."""
        try:
            reservations, counts = self.edcb.enum_tuner_reserve()
        except EdcbUnavailable as error:
            if self.config.edcb.required:
                logger.warning("EDCB に問い合わせできません: %s", error)
                return 0, None
            logger.debug("EDCB に問い合わせできません: %s", error)
            return None, None

        if driver not in counts:
            logger.debug("%s は EDCB に登録されていません。", driver)
            return None, None

        return free_until(
            reservations, counts[driver], driver, moment,
            guard=self.config.edcb.guard, needed=needed)

    def _watchdog(self, driver, index, elapsed):
        """Give a tuner back as EDCB's reservations come into range.

        The last job of a driver is the first to let go, so a round narrows
        instead of stopping outright.
        """
        window, blocker = self.free_window(driver, datetime.now(), needed=index)
        if window is None or window > 0:
            return None
        if blocker is not None:
            return f"EDCB の予約が近づきました ({blocker.title})"
        return "EDCB のチューナーに空きがなくなりました"

    # -- handover --------------------------------------------------------

    def _state_path(self, driver, index):
        return os.path.join(
            self.config.state_dir, f"{STATE_PREFIX}{_stem(driver)}-{index}.json")

    def _report_path(self, driver, index):
        return os.path.join(
            self.config.state_dir, f"report-{_stem(driver)}-{index}.csv")

    def _find_adoptable(self):
        found = []
        try:
            names = os.listdir(self.config.state_dir)
        except OSError:
            return found

        for name in sorted(names):
            if not name.startswith(STATE_PREFIX) or not name.endswith(".json"):
                continue
            runner = CaptureRunner(
                poll=self.config.edcb.poll,
                state_path=os.path.join(self.config.state_dir, name),
            )
            adoptable = runner.find_adoptable()
            if adoptable is not None:
                found.append((runner, adoptable))
        return found

    def adopt_pending(self):
        """Take over captures an earlier runner left running."""
        adoptable = self._adoptable or self._find_adoptable()
        self._adoptable = []
        if not adoptable:
            return []

        self.busy = True
        self._runners = [runner for runner, _ in adoptable]
        results = []
        try:
            with ThreadPoolExecutor(max_workers=len(adoptable)) as pool:
                futures = []
                for runner, entry in adoptable:
                    driver = entry[1].driver
                    self._activate(f"{driver} (引き継ぎ)")
                    futures.append(pool.submit(
                        runner.adopt, entry,
                        lambda elapsed, name=driver: self._watchdog(name, 1, elapsed)))
                for future in futures:
                    result = future.result()
                    self.history.apply_report(result.driver, result.report)
                    results.append(result)
        finally:
            self.busy = False
            self._runners = []
            self._active.clear()

        self.last_round = results
        self.last_finished = datetime.now()
        self._set_state("待機中")
        self.notify(results)
        return results

    # -- state -----------------------------------------------------------

    def _activate(self, label):
        with self._lock:
            self._active[label] = True
            labels = sorted(self._active)
        self._set_state("取得中: " + "、".join(labels))

    def _deactivate(self, label):
        with self._lock:
            self._active.pop(label, None)
            labels = sorted(self._active)
        if labels:
            self._set_state("取得中: " + "、".join(labels))

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


def _stem(driver):
    return re.sub(r"[^0-9A-Za-z._-]", "_", driver)
