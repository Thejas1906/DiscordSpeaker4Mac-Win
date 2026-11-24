from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from .theme import BAD, GOOD, WARN


def to_db(x: float) -> float:
    return -90.0 if x <= 1e-5 else 20 * math.log10(x)


class LevelMeter(QWidget):
    """Peak bar with RMS overlay on a -60..0 dBFS scale."""
    FLOOR = -60.0

    def __init__(self):
        super().__init__()
        self.peak = 0.0
        self.rms = 0.0
        self._hold = 0.0
        self.setMinimumHeight(40)
        self.setAccessibleName("Audio input level")

    def set_levels(self, peak: float, rms: float) -> None:
        self.peak, self.rms = peak, rms
        self._hold = max(peak, self._hold * 0.97)
        self.update()

    def _x(self, lin: float, w: float) -> float:
        return w * min(max((to_db(lin) - self.FLOOR) / -self.FLOOR, 0.0), 1.0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 4, self.width(), 14)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#111214"))
        p.drawRoundedRect(r, 7, 7)
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0.0, QColor(GOOD)); grad.setColorAt(0.75, QColor(GOOD))
        grad.setColorAt(0.88, QColor(WARN)); grad.setColorAt(1.0, QColor(BAD))
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(0, 4, self._x(self.peak, self.width()), 14), 7, 7)
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawRoundedRect(QRectF(0, 8, self._x(self.rms, self.width()), 6), 3, 3)
        hx = self._x(self._hold, self.width())
        if hx > 4:
            p.setPen(QPen(QColor("white"), 2)); p.drawLine(int(hx), 3, int(hx), 19)
        p.setPen(QColor("#6d6f78"))
        for db in (-48, -36, -24, -12, -6, 0):
            x = self.width() * (db - self.FLOOR) / -self.FLOOR
            p.drawText(int(x) - 12, 34, f"{db}")


class StatusPill(QWidget):
    def __init__(self, title: str):
        super().__init__()
        self._dot = QLabel("●")
        self._title = QLabel(title)
        self._title.setObjectName("sub")
        self._val = QLabel("—")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for w in (self._dot, self._title, self._val):
            w.setStyleSheet("background: transparent;")
            lay.addWidget(w)
        lay.addStretch(1)
        self.set("—", "#6d6f78")

    def set(self, text: str, color: str) -> None:
        self._val.setText(text)
        self._dot.setStyleSheet(f"color: {color}; background: transparent;")
        self.setAccessibleName(f"{self._title.text()}: {text}")
