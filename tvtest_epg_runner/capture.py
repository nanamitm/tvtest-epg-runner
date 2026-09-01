"""Run one TVTest EPG capture and watch over it while it runs."""

from __future__ import annotations

import json
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
    adopted: bool = False
    channel_count: int = 0
    report: list = field(default_factory=list)

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
        if self.adopted:
            base += " / 引き継ぎ"
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
            "adopted": self.adopted,
            "channels": self.channel_count,
            "captured": sum(1 for entry in self.report if entry["complete"]),
            "result": self.text,
        }


@dataclass
class CaptureRequest:
    driver: str
    timeout: int
    exe: str
    extra_args: list = field(default_factory=list)
    channels: str = ""          # /epgcapturech の指定 (空で現在の空間すべて)
    channel_count: int = 0
    report_path: str = ""


class CaptureRunner:
    """Launches TVTest and keeps an eye on it until it exits.

    ``watchdog`` is called every ``poll`` seconds with the seconds elapsed; it
    returns a reason string to abort the capture, or None to let it continue.
    """

    def __init__(self, poll=30, state_path=""):
        self.poll = poll
        # Written while a capture runs, so a runner that was killed can pick
        # the capture back up instead of leaving TVTest holding a tuner.
        self.state_path = state_path
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
        ]
        if request.channels:
            args += ["/epgcapturech", request.channels]
        if request.report_path:
            # 前回の内容が混ざらないよう、書き出す前に消しておく
            try:
                os.remove(request.report_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning("取得結果の書き出し先を消せません: %s", error)
            args += ["/epgcapturereport", request.report_path]
        args += list(request.extra_args)

        if request.channel_count:
            logger.info(
                "%s の番組表取得を開始します。(%d チャンネル / 制限時間 %s)",
                request.driver, request.channel_count, format_duration(request.timeout),
            )
        else:
            logger.info(
                "%s の番組表取得を開始します。(制限時間 %s)",
                request.driver, format_duration(request.timeout),
            )
        logger.debug("起動: %s", " ".join(args))

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

        self._write_state(request, process.pid, started)
        return self._monitor(process, request, started, watchdog)

    def find_adoptable(self):
        """The capture an earlier runner left behind, if it is still running.

        Returns ``(process, request, started)`` ready for :meth:`adopt`, or
        None when there is nothing to pick up.
        """
        state = self._read_state()
        if state is None:
            return None

        try:
            process = winevent.AttachedProcess(int(state["pid"]))
            request = CaptureRequest(
                driver=str(state["driver"]),
                timeout=int(state["timeout"]),
                exe=str(state["exe"]),
                channels=str(state.get("channels", "")),
                channel_count=int(state.get("channel_count", 0)),
                report_path=str(state.get("report_path", "")),
            )
            started = datetime.fromisoformat(state["started"])
        except (KeyError, TypeError, ValueError, LookupError):
            self._clear_state()
            return None

        if process.poll() is not None or not _same_file(process.image_path, request.exe):
            # The pid has been reused, or that capture is already over.
            process.close()
            self._clear_state()
            return None

        return process, request, started

    def adopt(self, adoptable, watchdog=None):
        process, request, started = adoptable
        logger.info(
            "前回の実行が残した %s の取得を引き継ぎます。(pid %s, %s 経過)",
            request.driver, process.pid,
            format_duration((datetime.now() - started).total_seconds()),
        )
        return self._monitor(process, request, started, watchdog, adopted=True)

    def _monitor(self, process, request, started, watchdog=None, adopted=False):
        self._stop.clear()
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
            self._clear_state()

        finished = datetime.now()
        result = CaptureResult(
            driver=request.driver, started=started, finished=finished,
            exit_code=process.returncode, timeout=request.timeout,
            cancelled=cancelled, cancel_reason=cancel_reason, adopted=adopted,
            channel_count=request.channel_count,
            report=read_report(request.report_path),
        )
        logger.info(
            "%s の番組表取得が終了しました。(%s, %s%s)",
            request.driver, result.text, format_duration(result.elapsed),
            f", {len(result.report)} チャンネル" if result.report else "",
        )
        return result

    # -- handover state --------------------------------------------------

    def _write_state(self, request, pid, started):
        if not self.state_path:
            return
        data = {
            "pid": pid,
            "driver": request.driver,
            "timeout": request.timeout,
            "exe": request.exe,
            "started": started.isoformat(timespec="seconds"),
            "channels": request.channels,
            "channel_count": request.channel_count,
            "report_path": request.report_path,
        }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False)
        except OSError as error:
            logger.warning("実行中の状態を保存できません: %s", error)

    def _read_state(self):
        if not self.state_path:
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            logger.warning("実行中の状態を読めません: %s", error)
            return None

    def _clear_state(self):
        if not self.state_path:
            return
        try:
            os.remove(self.state_path)
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning("実行中の状態を削除できません: %s", error)

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


def read_report(path):
    """Read the lines TVTest appended for the channels it walked.

    Each line is "日時,空間,チャンネル,完了,秒数,チャンネル数".
    """
    if not path:
        return []

    try:
        with open(path, "r", encoding="utf-8") as file:
            lines = file.readlines()
    except FileNotFoundError:
        return []
    except OSError as error:
        logger.warning("取得結果を読めません: %s (%s)", path, error)
        return []

    entries = []
    for line in lines:
        fields = line.strip().split(",")
        if len(fields) < 5:
            continue
        try:
            entries.append({
                "time": datetime.fromisoformat(fields[0]),
                "space": int(fields[1]),
                "channel": int(fields[2]),
                "key": f"{int(fields[1])}:{int(fields[2])}",
                "complete": fields[3] == "1",
                "seconds": int(fields[4]),
            })
        except ValueError:
            continue
    return entries


def _directory_of(exe):
    return os.path.dirname(os.path.abspath(exe)) or None


def _same_file(left, right):
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
