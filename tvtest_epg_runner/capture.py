"""Run one TVTest EPG capture and watch over it while it runs."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import winevent
from .util import format_duration

logger = logging.getLogger(__name__)

# TVTest's exit codes for /epgcaptureexit.
EXIT_COMPLETED = 0
EXIT_BEGIN_FAILED = 2
EXIT_INCOMPLETE = 3
EXIT_SYNC_FAILED = 4

EXIT_TEXT = {
    EXIT_COMPLETED: "完了",
    EXIT_BEGIN_FAILED: "取得を開始できませんでした",
    EXIT_INCOMPLETE: "未完了(中止または時間切れ)",
    EXIT_SYNC_FAILED: "EPG共有サーバへの送信に失敗",
}

# TVTest checks the time limit on its channel timer, which ticks every 15
# seconds, and then still has to save and flush before it exits.
CANCEL_GRACE = 180
CLOSE_GRACE = 60


@dataclass
class CaptureResult:
    driver: str
    started: datetime
    finished: datetime
    exit_code: int | None
    timeout: int
    cancelled: bool = False
    cancel_reason: str = ""
    skipped: str = ""
    detail: str = ""

    @property
    def elapsed(self):
        return int((self.finished - self.started).total_seconds())

    @property
    def ok(self):
        return self.exit_code == EXIT_COMPLETED

    @property
    def text(self):
        if self.skipped:
            return f"スキップ: {self.skipped}"
        if self.exit_code is None:
            return self.detail or "異常終了"
        base = EXIT_TEXT.get(self.exit_code, f"終了コード {self.exit_code}")
        if self.cancelled and self.cancel_reason:
            return f"{base} / {self.cancel_reason}"
        return base

    def as_dict(self):
        return {
            "driver": self.driver,
            "started": self.started.astimezone().isoformat(timespec="seconds"),
            "finished": self.finished.astimezone().isoformat(timespec="seconds"),
            "elapsed": self.elapsed,
            "timeout": self.timeout,
            "exit_code": self.exit_code,
            "cancelled": self.cancelled,
            "cancel_reason": self.cancel_reason,
            "skipped": self.skipped,
            "result": self.text,
        }


@dataclass
class CaptureRequest:
    driver: str
    timeout: int
    exe: str
    extra_args: list = field(default_factory=list)


class CaptureRunner:
    """Launches TVTest and keeps an eye on it until it exits.

    ``watchdog`` is called every ``poll`` seconds with the seconds elapsed; it
    returns a reason string to abort the capture, or None to let it continue.
    """

    def __init__(self, poll=30):
        self.poll = poll
        self._process = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def running(self):
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def pid(self):
        with self._lock:
            return self._process.pid if self._process is not None else None

    def request_cancel(self):
        """Ask the running capture to stop.  Safe to call from another thread."""
        self._stop.set()

    def run(self, request: CaptureRequest, watchdog=None):
        started = datetime.now()
        args = [
            request.exe,
            "/d", request.driver,
            "/epgcaptureexit",
            "/epgcapturetimeout", str(request.timeout),
        ] + list(request.extra_args)

        logger.info(
            "%s の番組表取得を開始します。(制限時間 %s)",
            request.driver, format_duration(request.timeout),
        )
        logger.debug("起動: %s", " ".join(args))

        self._stop.clear()
        try:
            process = subprocess.Popen(args, cwd=_directory_of(request.exe))
        except OSError as error:
            finished = datetime.now()
            logger.error("TVTest を起動できません: %s", error)
            return CaptureResult(
                driver=request.driver, started=started, finished=finished,
                exit_code=None, timeout=request.timeout,
                detail=f"起動に失敗しました: {error}",
            )

        with self._lock:
            self._process = process

        cancelled = False
        cancel_reason = ""
        try:
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    break

                elapsed = int((datetime.now() - started).total_seconds())
                reason = None
                if self._stop.is_set():
                    reason = "手動で中止しました"
                elif watchdog is not None:
                    reason = watchdog(elapsed)

                if reason is not None and not cancelled:
                    cancelled = True
                    cancel_reason = reason
                    self._abort(process, reason)

                time.sleep(1 if cancelled else self.poll)
        finally:
            with self._lock:
                self._process = None

        finished = datetime.now()
        result = CaptureResult(
            driver=request.driver, started=started, finished=finished,
            exit_code=process.returncode, timeout=request.timeout,
            cancelled=cancelled, cancel_reason=cancel_reason,
        )
        logger.info(
            "%s の番組表取得が終了しました。(%s, %s)",
            request.driver, result.text, format_duration(result.elapsed),
        )
        return result

    def _abort(self, process, reason):
        """Stop the capture, escalating only as far as it has to."""
        logger.info("番組表の取得を中止します: %s", reason)

        deadline = time.monotonic() + CANCEL_GRACE
        signaled = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            if not signaled:
                # The event only exists once the capture itself has begun, so
                # keep trying while TVTest is still starting up.
                signaled = winevent.signal_cancel(process.pid)
                if signaled:
                    logger.debug("中止イベントを送信しました。(pid %s)", process.pid)
            time.sleep(1)

        if process.poll() is not None:
            return

        logger.warning("中止イベントに応答しないため、ウィンドウを閉じます。")
        if winevent.post_close(process.pid):
            deadline = time.monotonic() + CLOSE_GRACE
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return
                time.sleep(1)

        logger.error("TVTest が終了しないため強制終了します。(pid %s)", process.pid)
        process.kill()


def _directory_of(exe):
    return os.path.dirname(os.path.abspath(exe)) or None
