"""The tray icon, drawn rather than shipped as a file."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

IDLE = "#3f8fd0"
RUNNING = "#37a85c"
FAILED = "#c84b3b"


def make_icon(color, size=64):
    """A dish and a dot, tinted by what the runner is doing."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRect(3, 3, size - 6, size - 6))

        pen = QPen(QColor("white"))
        pen.setWidth(max(3, size // 13))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        # 16 分の 1 度単位の角度指定
        painter.drawArc(QRect(13, 13, size - 26, size - 26), 200 * 16, 140 * 16)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("white"))
        dot = size // 8
        painter.drawEllipse(QRect(size // 2 - dot // 2, size // 2 + 2, dot, dot))
    finally:
        painter.end()

    return QIcon(pixmap)
