from __future__ import annotations

from functools import lru_cache

import lucide
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap


ICON_COLOR = "#3f3f46"
ACCENT_COLOR = "#0f6cbd"
SUCCESS_COLOR = "#18825d"


def _svg_pixmap(name: str, size: int, color: str, stroke_width: float = 1.8) -> QPixmap:
    svg = lucide._render_icon(
        name,
        size,
        stroke=color,
        stroke_width=stroke_width,
    ).encode("utf-8")
    pixmap = QPixmap()
    if not pixmap.loadFromData(svg, "SVG"):
        raise RuntimeError(f"无法加载图标: {name}")
    return pixmap


@lru_cache(maxsize=64)
def lucide_icon(name: str, color: str = ICON_COLOR) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        icon.addPixmap(_svg_pixmap(name, size, color))
    return icon


@lru_cache(maxsize=1)
def application_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        canvas = QPixmap(size, size)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ACCENT_COLOR))
        radius = max(3.0, size * 0.2)
        painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
        glyph_size = max(12, round(size * 0.64))
        glyph = _svg_pixmap("folder-sync", glyph_size, "#ffffff", 1.9)
        offset = (size - glyph_size) // 2
        painter.drawPixmap(offset, offset, glyph)
        painter.end()
        icon.addPixmap(canvas)
    return icon
