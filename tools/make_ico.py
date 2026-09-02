"""Write the application mark to an .ico for the packaged executable.

Qt draws the mark; the ICO container is assembled here so that packaging
needs nothing beyond what the application already depends on.
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray
from PySide6.QtWidgets import QApplication

from tvtest_epg_runner.ui.icons import IDLE, make_pixmap

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(size):
    pixmap = make_pixmap(IDLE, size)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.WriteOnly)
    pixmap.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def build(path):
    images = [(size, png_bytes(size)) for size in SIZES]

    # ICONDIR: 予約, 種別(1=icon), 画像数
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)

    entries = []
    for size, data in images:
        # 256 は 0 として書く決まり
        entries.append(struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32, len(data), offset))
        offset += len(data)

    with open(path, "wb") as file:
        file.write(header)
        for entry in entries:
            file.write(entry)
        for _, data in images:
            file.write(data)

    return path


if __name__ == "__main__":
    app = QApplication([])
    out = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tvtest_epg_runner", "ui", "app.ico")
    build(out)
    print("wrote", out, os.path.getsize(out), "bytes")
