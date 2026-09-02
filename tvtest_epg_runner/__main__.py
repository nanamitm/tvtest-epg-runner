"""Entry point: tray application, or a single capture round from the console."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from datetime import datetime

from . import config as config_module
from . import winevent
from .scheduler import Scheduler, next_run_after
from .util import format_duration

logger = logging.getLogger("tvtest_epg_runner")


def setup_logging(config, console=True):
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y/%m/%d %H:%M:%S")

    os.makedirs(os.path.dirname(os.path.abspath(config.log_file)), exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        config.log_file, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)


def run_once(scheduler, drivers):
    results = list(scheduler.adopt_pending())

    known = {driver.name for driver in scheduler.config.enabled_drivers}
    for name in sorted(set(drivers or []) - known):
        logger.error("設定にないドライバです: %s", name)
    if drivers and not (set(drivers) & known):
        return 1

    results += scheduler.run_round(drivers or None)
    if not results:
        return 1

    print()
    for result in results:
        print(f"{result.driver}: {result.text} ({format_duration(result.elapsed)})")

    if any(result.skipped for result in results):
        return 2
    return 0 if all(result.ok for result in results) else 3


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="tvtest-epg-runner",
        description="TVTest の番組表取得を EDCB の予約を避けながら定期実行します。")
    parser.add_argument("-c", "--config", help="設定ファイルのパス")
    parser.add_argument(
        "--once", nargs="*", metavar="DRIVER",
        help="常駐せずに1回だけ取得します。ドライバ名を指定すると対象を絞ります。")
    parser.add_argument(
        "--check", action="store_true",
        help="設定と EDCB への接続を確認して終了します。")
    args = parser.parse_args(argv)

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as error:
        print(f"設定エラー: {error}", file=sys.stderr)
        return 1

    setup_logging(config, console=args.once is not None or args.check)

    scheduler = Scheduler(config)

    if args.check:
        return check(scheduler, config)

    if args.once is not None:
        return run_once(scheduler, args.once)

    mutex = winevent.acquire_single_instance()
    if mutex is None:
        print("TVTest EPG Runner は既に起動しています。", file=sys.stderr)
        return 1

    from .syncserver import SyncServer
    from .ui.app import TrayApplication

    logger.info("TVTest EPG Runner を開始します。(設定 %s)", config.path)
    server = SyncServer(config.server)
    server.start()
    try:
        return TrayApplication(scheduler, config, server).run()
    finally:
        server.stop()


def check(scheduler, config):
    print(f"設定ファイル : {config.path}")
    print(f"TVTest       : {config.exe}")
    print(f"ログ         : {config.log_file}")
    if config.every:
        print(f"実行間隔     : {format_duration(config.every)}")
    else:
        print("実行時刻     : " + ", ".join(f"{t:%H:%M}" for t in config.times))
    print(f"次回実行     : {next_run_after(config, datetime.now()):%m/%d %H:%M}")
    print(f"通知先       : {config.addon.url or '(なし)'}")
    print()

    freshness = scheduler._addon_freshness()  # noqa: SLF001 - 表示のためだけ
    now = datetime.now()
    for driver in config.drivers:
        window, blocker = scheduler.free_window(
            driver.name, now, needed=driver.instances)
        state = "有効" if driver.enabled else "無効"
        if window is None:
            free = "EDCB 管理外"
        elif window <= 0:
            free = "空きなし"
        else:
            free = f"空き {format_duration(window)}"
        print(
            f"{driver.name} [{state}] {free} / 制限時間 "
            f"{format_duration(driver.timeout)} / 同時 {driver.instances} 本")
        if blocker is not None:
            print(f"    次の予約: {blocker}")

        groups = scheduler._groups_for(driver)  # noqa: SLF001 - 表示のためだけ
        if groups:
            captured, total = scheduler.history.summary(driver.name, groups)
            ordered = scheduler.history.order(driver.name, groups, freshness)
            print(f"    チャンネル {total} / 取得済み {captured}")
            print("    次に取得: " + ", ".join(
                f"{g.key}({g.name})" for g in ordered[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
