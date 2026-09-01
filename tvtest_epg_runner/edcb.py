"""Read EDCB's reservations so a capture never takes a tuner a recording needs.

EpgTimerSrv's HTTP API groups the reservations by the BonDriver they are
assigned to, which is exactly the granularity a capture needs: one entry per
tuner instance, so a driver with several instances can still lend one out.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

UNASSIGNED_TUNER_ID = "-1"


@dataclass(frozen=True)
class Reservation:
    id: str
    title: str
    start: datetime
    end: datetime
    tuner_id: str
    tuner_name: str

    def __str__(self):
        return (
            f"{self.start:%m/%d %H:%M}-{self.end:%H:%M} {self.title} "
            f"({self.tuner_name} #{self.tuner_id})"
        )


class EdcbUnavailable(Exception):
    """EpgTimerSrv could not be reached or answered with something unusable."""


class EdcbClient:
    def __init__(self, url, timeout=10, default_start_margin=30, default_end_margin=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.default_start_margin = default_start_margin
        self.default_end_margin = default_end_margin

    def enum_tuner_reserve(self):
        """Return (reservations, tuner_counts).

        ``tuner_counts`` maps a BonDriver file name to how many tuner instances
        EDCB has for it, which is how many recordings it can run at once.
        """
        try:
            response = requests.get(
                f"{self.url}/api/EnumTunerReserveInfo", timeout=self.timeout
            )
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except (requests.RequestException, ElementTree.ParseError) as error:
            raise EdcbUnavailable(str(error)) from error

        reservations = []
        tuner_counts = {}

        for tuner in root.iter("tuner"):
            tuner_id = _text(tuner, "tunerID")
            tuner_name = _text(tuner, "tunerName")
            if tuner_id == UNASSIGNED_TUNER_ID:
                # EDCB parks reservations it cannot assign under a pseudo tuner
                # named "チューナー不足".  They are already failing, so holding a
                # tuner back for them would not help anybody.
                continue
            tuner_counts[tuner_name] = tuner_counts.get(tuner_name, 0) + 1
            for info in tuner.iter("reserveinfo"):
                reservation = self._parse_reservation(info, tuner_id, tuner_name)
                if reservation is not None:
                    reservations.append(reservation)

        return reservations, tuner_counts

    def _parse_reservation(self, info, tuner_id, tuner_name):
        setting = info.find("recsetting")
        if setting is not None and _text(setting, "recEnabled") == "0":
            return None

        try:
            start = datetime.strptime(
                f"{_text(info, 'startDate')} {_text(info, 'startTime')}",
                "%Y/%m/%d %H:%M:%S",
            )
            duration = int(_text(info, "duration") or 0)
        except ValueError:
            logger.debug("予約の日時を解釈できませんでした: %s", _text(info, "ID"))
            return None

        start_margin = self.default_start_margin
        end_margin = self.default_end_margin
        if setting is not None and _text(setting, "useMargineFlag") == "1":
            start_margin = _int(setting, "startMargine", start_margin)
            end_margin = _int(setting, "endMargine", end_margin)

        # A positive margin starts the recording early, so it eats into the
        # time a capture may use.  A negative one is a late start; ignore it
        # rather than pretending the tuner is free for longer than it is.
        return Reservation(
            id=_text(info, "ID"),
            title=_text(info, "title"),
            start=start - timedelta(seconds=max(0, start_margin)),
            end=start + timedelta(seconds=duration + max(0, end_margin)),
            tuner_id=tuner_id,
            tuner_name=tuner_name,
        )


def free_until(reservations, tuner_count, driver, since, guard=0, horizon=24 * 3600):
    """How long ``driver`` stays free from ``since``, and what takes it next.

    Returns ``(seconds, blocking_reservation)``.  The driver counts as free
    while fewer than ``tuner_count`` of its instances are busy, so a driver
    with ten tuners can lend one out during a single recording.  ``guard`` is
    subtracted from the answer, the way EDCB's own NGEpgCapTime keeps its EPG
    capture away from an upcoming recording.
    """
    if tuner_count <= 0:
        return 0, None

    limit = since + timedelta(seconds=horizon)
    upcoming = sorted(
        (r for r in reservations if r.tuner_name == driver and r.end > since and r.start < limit),
        key=lambda r: r.start,
    )

    # Walk the reservation boundaries and stop at the first moment where every
    # tuner instance of this driver is taken.
    for candidate in upcoming:
        moment = max(candidate.start, since)
        busy = sum(1 for r in upcoming if r.start <= moment < r.end)
        if busy >= tuner_count:
            seconds = int((candidate.start - since).total_seconds()) - guard
            return max(0, seconds), candidate

    return max(0, horizon - guard), None


def _text(element, name, default=""):
    if element is None:
        return default
    child = element.find(name)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _int(element, name, default):
    try:
        return int(_text(element, name))
    except ValueError:
        return default
