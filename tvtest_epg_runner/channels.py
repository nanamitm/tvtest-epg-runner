"""Read a BonDriver's channel file and group it the way a capture walks it.

TVTest visits one physical channel at a time and picks up every service on
that transport stream at once, so a channel group — one tuning space and one
channel — is the unit the work is divided into.
"""

from __future__ import annotations

import codecs
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 衛星は1周に時間がかかるので、実測が無いときの見積もりを分ける
SATELLITE_NETWORK_IDS = range(1, 40)  # BS/CS110/advanced satellite


@dataclass(frozen=True)
class Service:
    name: str
    network_id: int
    transport_stream_id: int
    service_id: int

    @property
    def key(self):
        return (self.network_id, self.transport_stream_id, self.service_id)


@dataclass
class ChannelGroup:
    space: int
    channel: int
    services: list = field(default_factory=list)

    @property
    def key(self):
        return f"{self.space}:{self.channel}"

    @property
    def name(self):
        return self.services[0].name if self.services else self.key

    @property
    def satellite(self):
        return any(s.network_id in SATELLITE_NETWORK_IDS for s in self.services)

    def __str__(self):
        return f"{self.key} {self.name}"


def channel_file_for(exe, driver):
    """The .ch2 TVTest reads for this BonDriver, next to the executable."""
    directory = os.path.dirname(os.path.abspath(exe))
    stem = os.path.splitext(driver)[0]
    return os.path.join(directory, stem + ".ch2")


def load_groups(path):
    """Parse a .ch2 into the channel groups a capture would visit.

    Disabled channels are left out, the way TVTest leaves them out.
    """
    groups = {}

    try:
        text = _read_text(path)
    except FileNotFoundError:
        logger.warning("チャンネルファイルがありません: %s", path)
        return []
    except (OSError, UnicodeDecodeError) as error:
        logger.warning("チャンネルファイルを読めません: %s (%s)", path, error)
        return []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue

        fields = line.split(",")
        if len(fields) < 3:
            continue

        try:
            space = int(fields[1])
            channel = int(fields[2])
            service_id = int(fields[5]) if len(fields) > 5 else 0
            network_id = int(fields[6]) if len(fields) > 6 else 0
            transport_stream_id = int(fields[7]) if len(fields) > 7 else 0
            enabled = int(fields[8]) if len(fields) > 8 else 1
        except ValueError:
            continue

        if not enabled:
            continue

        group = groups.get((space, channel))
        if group is None:
            group = ChannelGroup(space=space, channel=channel)
            groups[(space, channel)] = group
        group.services.append(Service(
            name=fields[0],
            network_id=network_id,
            transport_stream_id=transport_stream_id,
            service_id=service_id,
        ))

    return [groups[key] for key in sorted(groups)]


def _read_text(path):
    """Read a .ch2, which TVTest writes as UTF-16, UTF-8 or Shift_JIS."""
    with open(path, "rb") as file:
        data = file.read()

    if data.startswith(codecs.BOM_UTF16_LE):
        return data.decode("utf-16-le")
    if data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16-be")
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")

    # 印の無い UTF-16 も読む。取り違えると1行も拾えず、ドライバが黙って
    # 対象から外れてしまうため。
    head = data[:512]
    if head.count(b"\x00") > len(head) // 4:
        return data.decode("utf-16-le" if head[1:2] == b"\x00" else "utf-16-be",
                           errors="replace")

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp932", errors="replace")


def parse_spec(spec):
    """Parse TVTest's /epgcapturech syntax into match rules.

    Returns a list of (space, first, last) where None means "any".
    """
    rules = []
    for item in str(spec).replace(" ", "").split(","):
        if not item:
            continue
        space_text, _, channel_text = item.partition(":")
        space = None if space_text == "*" else int(space_text)
        if not channel_text or channel_text == "*":
            rules.append((space, None, None))
            continue
        first_text, _, last_text = channel_text.partition("-")
        first = int(first_text)
        last = int(last_text) if last_text else first
        if last < first:
            raise ValueError(f"チャンネルの範囲が逆です: {item}")
        rules.append((space, first, last))
    if not rules:
        raise ValueError("チャンネルの指定が空です")
    return rules


def select(groups, spec):
    """The groups a /epgcapturech specification would keep."""
    if not spec or spec.strip() in ("*", "*:*"):
        return list(groups)

    rules = parse_spec(spec)
    selected = []
    for group in groups:
        for space, first, last in rules:
            if space is not None and space != group.space:
                continue
            if first is None or first <= group.channel <= last:
                selected.append(group)
                break
    return selected


def to_spec(groups):
    """The /epgcapturech argument that selects exactly these groups."""
    return ",".join(f"{group.space}:{group.channel}" for group in groups)
