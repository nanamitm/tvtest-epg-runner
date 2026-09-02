"""The application's mark, drawn rather than shipped as a file.

The same dish and dot serves as the tray icon and as the window icon, so a
dialog is recognisable as belonging to whatever is sitting in the tray.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

IDLE = "#3f8fd0"
RUNNING = "#37a85c"
FAILED = "#c84b3b"

# 窓・タスクバー・通知領域が使う大きさ
ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def make_pixmap(color, size):
    """The mark at one size, drawn in proportion so it holds up when small."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)

        # 外周の円
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        inset = size * 0.05
        painter.drawEllipse(QRectF(inset, inset, size - inset * 2, size - inset * 2))

        # パラボラを思わせる弧
        pen = QPen(QColor("white"))
        pen.setWidthF(max(1.5, size * 0.08))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        arc = size * 0.22
        # 角度は 16 分の 1 度単位
        painter.drawArc(
            QRectF(arc, arc, size - arc * 2, size - arc * 2), 200 * 16, 140 * 16)

        # 受信点
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("white"))
        dot = size * 0.16
        painter.drawEllipse(
            QRectF(size / 2 - dot / 2, size * 0.54, dot, dot))
    finally:
        painter.end()

    return pixmap


def make_icon(color):
    """The mark as an icon carrying every size a window might ask for."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(make_pixmap(color, size))
    return icon
