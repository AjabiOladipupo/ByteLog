"""Stacked download/upload bar chart built on pyqtgraph.

Bars are painted in device pixels (not data units) so the rounded tops stay round and
the bar width can be capped regardless of the view's scale. Download sits at the
bottom, upload on top. Pan, zoom and the context menu are disabled; hovering a bar shows
a tooltip with the exact values.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QStyleOptionGraphicsItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from bytelog.storage.models import Bucket
from bytelog.ui import theme
from bytelog.ui.fonts import mono_font
from bytelog.units import GB, MB, format_bytes

BAR_FRACTION = 0.62  # of the slot width
MAX_BAR_PX = 30.0
TOP_RADIUS_PX = 3.0
Y_TICKS = 4


def nice_axis(max_value: int) -> tuple[list[float], str, int]:
    """Gridline values in bytes, the display unit and its divisor for a data maximum."""
    unit, divisor = ("GB", GB) if max_value >= GB else ("MB", MB)
    scaled = max(max_value / divisor, 1.0 if unit == "MB" else 0.1)
    raw_step = scaled / Y_TICKS
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(
        m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude * Y_TICKS >= scaled * 0.999
    )
    count = math.ceil(scaled / step - 1e-9)
    return [i * step * divisor for i in range(count + 1)], unit, divisor


def tick_label(value: float, unit: str, divisor: int) -> str:
    if value == 0:
        return "0"
    scaled = value / divisor
    # Two decimals: steps of 0.25 must not print as 0.2 / 0.8.
    text = f"{scaled:.2f}".rstrip("0").rstrip(".") if scaled < 10 else f"{scaled:.0f}"
    return f"{text} {unit}"


def tooltip_html(bucket: Bucket) -> str:
    t = theme.tokens()

    def amount(value: int) -> str:
        """Rounded figure plus the exact byte count, which the cards cannot show."""
        exact = f'<span style="color:{t.muted}">{value:,} bytes</span>'
        return f"<b>{format_bytes(value)}</b>&nbsp;&nbsp;{exact}"

    def row(color: str, name: str, value: int) -> str:
        swatch = f'<span style="color:{color}">&#9632;</span>'
        return f"{swatch} {name}&nbsp;&nbsp;{amount(value)}"

    return (
        f"<b>{bucket.tooltip_title}</b><br>"
        f"{row(t.download, 'Download', bucket.rx)}<br>"
        f"{row(t.upload, 'Upload', bucket.tx)}<br>"
        f"Total&nbsp;&nbsp;{amount(bucket.total)}"
    )


class _BarsItem(pg.GraphicsObject):
    """Draws the gridlines, hover band and stacked bars for all buckets."""

    def __init__(self) -> None:
        super().__init__()
        self.buckets: list[Bucket] = []
        self.grid: list[float] = []
        self.y_top = 1.0
        self.hover: int | None = None

    def set_data(self, buckets: list[Bucket], grid: list[float], y_top: float) -> None:
        self.prepareGeometryChange()
        self.buckets = buckets
        self.grid = grid
        self.y_top = y_top
        self.hover = None
        self.update()

    def set_hover(self, index: int | None) -> None:
        if index != self.hover:
            self.hover = index
            self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt API
        return QRectF(-0.5, 0.0, max(len(self.buckets), 1), self.y_top)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        t = theme.tokens()
        transform = painter.transform()
        count = len(self.buckets)
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        def to_px(x: float, y: float) -> QPointF:
            return transform.map(QPointF(x, y))

        left = to_px(-0.5, 0).x()
        right = to_px(max(count, 1) - 0.5, 0).x()
        grid_pen = pg.mkPen(QColor(t.track), width=1)
        painter.setPen(grid_pen)
        for value in self.grid:
            y = round(to_px(0, value).y()) + 0.5
            painter.drawLine(QPointF(left, y), QPointF(right, y))

        if count:
            slot = abs(to_px(1, 0).x() - to_px(0, 0).x())
            width = max(2.0, min(slot * BAR_FRACTION, MAX_BAR_PX))
            base_y = to_px(0, 0).y()
            painter.setPen(Qt.PenStyle.NoPen)
            if self.hover is not None and 0 <= self.hover < count:
                center = to_px(self.hover, 0).x()
                band = QRectF(center - slot / 2, to_px(0, self.y_top).y(), slot, 0)
                band.setBottom(base_y)
                hover_color = QColor(t.muted)
                hover_color.setAlphaF(0.08)
                painter.setBrush(hover_color)
                painter.drawRoundedRect(band, 6, 6)
            for i, bucket in enumerate(self.buckets):
                if bucket.total <= 0:
                    continue
                center = to_px(i, 0).x()
                top_y = to_px(0, bucket.total).y()
                rx_y = to_px(0, bucket.rx).y()
                height = max(base_y - top_y, 1.5)
                rect = QRectF(center - width / 2, base_y - height, width, height)
                radius = min(TOP_RADIUS_PX, width / 2, height / 2)
                path = QPainterPath()
                path.addRoundedRect(rect, radius, radius)
                # Square off the bottom corners.
                path.addRect(QRectF(rect.left(), rect.bottom() - radius, rect.width(), radius))
                path.setFillRule(Qt.FillRule.WindingFill)
                painter.save()
                painter.setClipPath(path)
                painter.fillRect(
                    QRectF(rect.left(), rx_y, width, base_y - rx_y), QColor(t.download)
                )
                if bucket.tx > 0:
                    painter.fillRect(
                        QRectF(rect.left(), rect.top(), width, rx_y - rect.top()),
                        QColor(t.upload),
                    )
                painter.restore()
        painter.restore()


class StackedBarChart(QWidget):
    """``set_buckets(list[Bucket])`` replaces the data; ``tallest()`` gives the max bar."""

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        # Prefer the minimum height and grow by stretch. A taller preferred height makes a
        # scroll area whose layout has height-for-width (wrapped labels) scroll needlessly.
        return QSize(super().sizeHint().width(), self.minimumHeight())

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buckets: list[Bucket] = []
        self._x_labels: list[tuple[float, str]] = []
        self.setMinimumHeight(220)

        self.plot = pg.PlotWidget()
        self.plot.setAntialiasing(True)
        self.plot.setFrameShape(QFrame.Shape.NoFrame)
        item = self.plot.getPlotItem()
        item.setMenuEnabled(False)
        item.hideButtons()
        item.setMouseEnabled(x=False, y=False)
        item.vb.setMouseEnabled(x=False, y=False)
        item.vb.disableAutoRange()
        item.layout.setContentsMargins(0, 6, 4, 0)
        self._left = item.getAxis("left")
        self._bottom = item.getAxis("bottom")
        for axis in (self._left, self._bottom):
            axis.setStyle(tickLength=0, autoExpandTextSpace=True)
        self._left.setStyle(tickTextOffset=10)
        self._bottom.setStyle(tickTextOffset=8)
        self._bars = _BarsItem()
        item.addItem(self._bars)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot)

        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot.viewport().setMouseTracking(True)
        self.apply_theme()
        self.set_buckets([])

    # -- public API -------------------------------------------------------------------

    @property
    def buckets(self) -> list[Bucket]:
        return list(self._buckets)

    def set_buckets(self, buckets: Sequence[Bucket]) -> None:
        buckets = list(buckets)
        if buckets == self._buckets and self._x_labels:
            return
        self._buckets = buckets
        peak = max((b.total for b in buckets), default=0)
        grid, unit, divisor = nice_axis(peak)
        y_top = grid[-1] if grid[-1] > 0 else MB
        self._bars.set_data(buckets, grid, y_top)
        item = self.plot.getPlotItem()
        count = max(len(buckets), 1)
        item.setXRange(-0.6, count - 0.4, padding=0)
        item.setYRange(0, y_top * 1.02, padding=0)
        self._left.setTicks([[(v, tick_label(v, unit, divisor)) for v in grid], []])
        self._x_labels = [(float(i), b.label) for i, b in enumerate(buckets)]
        self._update_x_ticks()

    def tallest(self) -> Bucket | None:
        best: Bucket | None = None
        for bucket in self._buckets:
            if bucket.total > 0 and (best is None or bucket.total > best.total):
                best = bucket
        return best

    def apply_theme(self) -> None:
        t = theme.tokens()
        self.plot.setBackground(QColor(t.card))
        font = mono_font(8.0)
        for axis in (self._left, self._bottom):
            axis.setPen(pg.mkPen(QColor(t.border), width=1))
            axis.setTextPen(pg.mkPen(QColor(t.muted)))
            axis.setTickFont(font)
        self._left.setPen(pg.mkPen(None))
        self._left.setTextPen(pg.mkPen(QColor(t.muted)))
        self._bars.update()

    # -- internals --------------------------------------------------------------------

    def _update_x_ticks(self) -> None:
        if not self._x_labels:
            self._bottom.setTicks([[]])
            return
        width = max(self.plot.width() - 70, 50)
        longest = max(len(label) for _, label in self._x_labels)
        label_px = longest * 7 + 12
        per_slot = width / len(self._x_labels)
        step = max(1, math.ceil(label_px / max(per_slot, 1)))
        shown = [tick for i, tick in enumerate(self._x_labels) if i % step == 0]
        self._bottom.setTicks([shown, []])

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._update_x_ticks()

    def _index_at(self, scene_pos: QPointF) -> int | None:
        vb = self.plot.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            return None
        point = vb.mapSceneToView(scene_pos)
        index = round(point.x())
        if 0 <= index < len(self._buckets) and abs(point.x() - index) <= 0.5:
            return index
        return None

    def _on_mouse_moved(self, scene_pos: QPointF) -> None:
        index = self._index_at(scene_pos)
        self._bars.set_hover(index)
        if index is None:
            QToolTip.hideText()
            return
        QToolTip.showText(QCursor.pos(), tooltip_html(self._buckets[index]), self.plot)

    def leaveEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        self._bars.set_hover(None)
        QToolTip.hideText()


class _Swatch(QWidget):
    def __init__(self, role: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._role = role
        self.setFixedSize(10, 10)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(getattr(theme.tokens(), self._role)))
        painter.drawRoundedRect(QRectF(self.rect()), 3, 3)
        painter.end()


class ChartLegend(QWidget):
    """Download / Upload swatches; repaints from the current theme tokens."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        for i, (role, text) in enumerate((("download", "Download"), ("upload", "Upload"))):
            if i:
                layout.addSpacing(12)
            layout.addWidget(_Swatch(role), 0, Qt.AlignmentFlag.AlignVCenter)
            label = QLabel(text)
            label.setProperty("muted", True)
            layout.addWidget(label)
