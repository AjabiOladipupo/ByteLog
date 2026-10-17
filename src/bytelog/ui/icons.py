"""Programmatic icons (QPainter), so the UI never depends on image assets."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from bytelog.paths import resource_path
from bytelog.ui import theme

# Bar heights as fractions of the drawing area: a small rising bar chart.
_BARS = (0.45, 0.8, 0.6, 1.0)


def _draw_bars(
    painter: QPainter, area: QRectF, bar_color: QColor, top_color: QColor | None
) -> None:
    count = len(_BARS)
    gap = area.width() * 0.12
    width = (area.width() - gap * (count - 1)) / count
    radius = width * 0.3
    painter.setPen(Qt.PenStyle.NoPen)
    for i, fraction in enumerate(_BARS):
        height = area.height() * fraction
        x = area.left() + i * (width + gap)
        bar = QRectF(x, area.bottom() - height, width, height)
        painter.setBrush(bar_color)
        painter.drawRoundedRect(bar, radius, radius)
        if top_color is not None:
            cap = QRectF(bar.left(), bar.top(), bar.width(), bar.height() * 0.3)
            painter.setBrush(top_color)
            painter.drawRoundedRect(cap, radius, radius)


def logo_pixmap(size: int = 22, device_pixel_ratio: float = 2.0) -> QPixmap:
    """Sidebar logo: blue rounded tile with white/orange bars."""
    t = theme.tokens()
    pixmap = QPixmap(round(size * device_pixel_ratio), round(size * device_pixel_ratio))
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    tile = QRectF(0, 0, size, size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.LIGHT.download))
    painter.drawRoundedRect(tile, size * 0.26, size * 0.26)
    inset = size * 0.24
    _draw_bars(
        painter, tile.adjusted(inset, inset, -inset, -inset), QColor("#FFFFFF"), QColor(t.upload)
    )
    painter.end()
    return pixmap


def _tile_pixmap(size: int, paused: bool) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    tile = QRectF(0, 0, size, size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.LIGHT.sidebar if paused else theme.LIGHT.download))
    painter.drawRoundedRect(tile, size * 0.22, size * 0.22)
    inset = size * 0.18
    bars = QColor(theme.LIGHT.sidebar_muted) if paused else QColor("#FFFFFF")
    top = None if paused else QColor(theme.LIGHT.upload)
    _draw_bars(painter, tile.adjusted(inset, inset, -inset, -inset), bars, top)
    painter.end()
    return pixmap


def app_icon(paused: bool = False) -> QIcon:
    """Window/tray icon. Prefers the bundled .ico when present (not for the paused look)."""
    if not paused:
        bundled = resource_path("icon.ico")
        if bundled.is_file():
            icon = QIcon(str(bundled))
            if not icon.isNull():
                return icon
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 256):
        icon.addPixmap(_tile_pixmap(size, paused))
    return icon


def tray_icon(paused: bool = False) -> QIcon:
    return app_icon(paused)


# -- sidebar navigation glyphs (16 px line icons) -------------------------------------------


def _glyph_overview(p: QPainter) -> None:
    for x, y in ((2.5, 2.5), (9, 2.5), (2.5, 9), (9, 9)):
        p.drawRoundedRect(QRectF(x, y, 4.5, 4.5), 1.2, 1.2)


def _glyph_reports(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 1.8, 10, 12.4), 1.8, 1.8)
    for y in (5.5, 8.2, 10.9):
        p.drawLine(QPointF(5.6, y), QPointF(10.4 if y < 10 else 8.6, y))


def _glyph_networks(p: QPainter) -> None:
    center = QPointF(8, 12.6)
    for r in (3.2, 6.4, 9.6):
        rect = QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r)
        p.drawArc(rect, 45 * 16, 90 * 16)
    p.setBrush(p.pen().color())
    p.drawEllipse(center, 1.0, 1.0)


def _glyph_plans(p: QPainter) -> None:
    rect = QRectF(2, 3.5, 12, 12)
    p.drawArc(rect, 0, 180 * 16)
    p.drawLine(QPointF(2, 9.5), QPointF(14, 9.5))
    p.drawLine(QPointF(8, 9.5), QPointF(11, 5.8))


def _glyph_settings(p: QPainter) -> None:
    for y, knob in ((4, 10.5), (8, 5.5), (12, 9)):
        p.drawLine(QPointF(2, y), QPointF(14, y))
        p.save()
        p.setBrush(p.pen().color())
        p.drawEllipse(QPointF(knob, y), 1.7, 1.7)
        p.restore()


_GLYPHS = {
    "overview": _glyph_overview,
    "reports": _glyph_reports,
    "networks": _glyph_networks,
    "plans": _glyph_plans,
    "settings": _glyph_settings,
}


def _glyph_pixmap(key: str, color: QColor, ratio: float) -> QPixmap:
    pixmap = QPixmap(round(16 * ratio), round(16 * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    _GLYPHS[key](painter)
    painter.end()
    return pixmap


def nav_icon(key: str) -> QIcon:
    """Muted glyph normally, white when the nav item is checked or hovered."""
    t = theme.tokens()
    icon = QIcon()
    if key not in _GLYPHS:
        return icon
    for ratio in (1.0, 2.0):
        muted = _glyph_pixmap(key, QColor(t.sidebar_muted), ratio)
        white = _glyph_pixmap(key, QColor("#FFFFFF"), ratio)
        icon.addPixmap(muted, QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(white, QIcon.Mode.Normal, QIcon.State.On)
        icon.addPixmap(white, QIcon.Mode.Active, QIcon.State.Off)
        icon.addPixmap(white, QIcon.Mode.Active, QIcon.State.On)
    return icon
