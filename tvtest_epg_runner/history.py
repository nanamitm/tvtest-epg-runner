"""What was captured when, so the stalest channels go first.

Every channel group carries two times: when a capture last visited it, and
when a capture last finished it.  Ordering uses the visit, which is what keeps
a channel that always times out from starving the rest; the completion is what
the tray and the add-on report.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

DISTANT_PAST = datetime.min

# 実測が貯まるまでの1チャンネルあたりの見積もり
DEFAULT_TERRESTRIAL_SECONDS = 130
DEFAULT_SATELLITE_SECONDS = 200


class CaptureHistory:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._drivers = {}
        self._load()

    # -- 保存と読み込み ----------------------------------------------------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            logger.warning("取得履歴を読めません: %s (%s)", self.path, error)
            return

        drivers = data.get("drivers")
        if isinstance(drivers, dict):
            self._drivers = drivers

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            temporary = self.path + ".new"
            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(
                    {"saved_at": _now_text(), "drivers": self._drivers},
                    file, ensure_ascii=False, indent=1,
                )
            os.replace(temporary, self.path)
        except OSError as error:
            logger.warning("取得履歴を保存できません: %s", error)

    def _entry(self, driver, key):
        driver_entries = self._drivers.setdefault(driver, {})
        return driver_entries.setdefault(key, {})

    # -- 記録 --------------------------------------------------------------

    def mark_attempted(self, driver, groups, when=None):
        """Note that a capture was pointed at these groups."""
        when = when or datetime.now()
        with self._lock:
            for group in groups:
                self._entry(driver, group.key)["attempted_at"] = _text(when)
            self._save()

    def apply_report(self, driver, entries):
        """Take in what TVTest wrote about the channels it actually walked."""
        if not entries:
            return
        with self._lock:
            for entry in entries:
                record = self._entry(driver, entry["key"])
                record["attempted_at"] = _text(entry["time"])
                record["seconds"] = entry["seconds"]
                if entry["complete"]:
                    record["captured_at"] = _text(entry["time"])
            self._save()

    def mark_captured(self, driver, groups, when=None):
        """Note a finished capture for groups TVTest did not report one by one.

        A capture that ends complete has been round every group it was given,
        even those it merged into one visit.
        """
        when = when or datetime.now()
        with self._lock:
            for group in groups:
                record = self._entry(driver, group.key)
                record["attempted_at"] = _text(when)
                record["captured_at"] = max(record.get("captured_at", ""), _text(when))
            self._save()

    # -- 参照 --------------------------------------------------------------

    def attempted_at(self, driver, group):
        return _time(self._drivers.get(driver, {}).get(group.key, {}).get("attempted_at"))

    def captured_at(self, driver, group):
        return _time(self._drivers.get(driver, {}).get(group.key, {}).get("captured_at"))

    def order(self, driver, groups, freshness=None):
        """The groups, least recently touched first.

        ``freshness`` maps a group key to a time the data was refreshed by
        someone else — the add-on's view of the LAN — and counts the same as
        having captured it then.
        """
        freshness = freshness or {}

        def key(group):
            seen = self.attempted_at(driver, group)
            elsewhere = freshness.get(group.key, DISTANT_PAST)
            return (max(seen, elsewhere), group.space, group.channel)

        return sorted(groups, key=key)

    def estimate_seconds(self, driver, group):
        """How long this group is likely to take, from what it took before."""
        record = self._drivers.get(driver, {}).get(group.key, {})
        seconds = record.get("seconds")
        if isinstance(seconds, int) and seconds > 0:
            return seconds

        measured = [
            entry["seconds"]
            for entry in self._drivers.get(driver, {}).values()
            if isinstance(entry.get("seconds"), int) and entry["seconds"] > 0
        ]
        if measured:
            measured.sort()
            return measured[len(measured) // 2]

        return DEFAULT_SATELLITE_SECONDS if group.satellite else DEFAULT_TERRESTRIAL_SECONDS

    def summary(self, driver, groups):
        """(captured, total) for the tray and the log."""
        captured = sum(
            1 for group in groups if self.captured_at(driver, group) > DISTANT_PAST)
        return captured, len(groups)


def _now_text():
    return _text(datetime.now())


def _text(when):
    return when.isoformat(timespec="seconds")


def _time(text):
    if not text:
        return DISTANT_PAST
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return DISTANT_PAST
