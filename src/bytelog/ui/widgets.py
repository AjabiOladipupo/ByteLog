"""Small reusable widgets that follow the design tokens."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from bytelog.ui import theme


class Card(QFrame):
    """White rounded card with a 1px border. Put content in ``card.body``."""

    def __init__(self, title: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(8)
        self.title_label: QLabel | None = None
        if title:
            self.title_label = QLabel(title)
            self.title_label.setObjectName("CardTitle")
            self.body.addWidget(self.title_label)


def muted_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setProperty("muted", True)
    return label


def mono_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setProperty("mono", True)
    return label


def primary_button(text: str, parent: QWidget | None = None) -> QPushButton:
    button = QPushButton(text, parent)
    button.setProperty("primary", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def danger_button(text: str, parent: QWidget | None = None) -> QPushButton:
    button = QPushButton(text, parent)
    button.setProperty("danger", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class ProgressBar(QWidget):
    """Thin rounded progress bar; ``value`` is a fraction, clamped to [0, 1] when drawn."""

    def __init__(
        self, color: str | None = None, height: int = 8, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._color = color
        self.setFixedHeight(height)

    def set_value(self, fraction: float) -> None:
        self._value = fraction
        self.update()

    def set_color(self, color: str | None) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        t = theme.tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        painter.setBrush(QColor(t.track))
        painter.drawRoundedRect(rect, radius, radius)
        fraction = max(0.0, min(1.0, self._value))
        if fraction > 0:
            filled = QRectF(
                rect.x(), rect.y(), max(rect.height(), rect.width() * fraction), rect.height()
            )
            painter.setBrush(QColor(self._color or t.download))
            painter.drawRoundedRect(filled, radius, radius)
        painter.end()


class Dot(QWidget):
    """Small status dot."""

    def __init__(self, color: str, size: int = 9, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._color))
        painter.drawEllipse(self.rect())
        painter.end()


def repolish(widget: QWidget) -> None:
    """Re-apply QSS after changing a dynamic property."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
