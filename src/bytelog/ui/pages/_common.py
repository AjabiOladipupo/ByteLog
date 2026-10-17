"""Layout and formatting helpers shared by the secondary pages (networks, plans, reports,
settings). Kept private to ``ui/pages`` so the foundation widgets stay small."""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, tzinfo
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from bytelog import timeutil
from bytelog.ui import theme
from bytelog.ui.widgets import muted_label

KIND_LABELS: dict[str, str] = {
    "wifi": "Wi-Fi",
    "ethernet": "Ethernet",
    "cellular": "Cellular",
    "wifi_unknown": "Wi-Fi (name unavailable)",
    "other": "Other",
}

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class PageScaffold:
    content: QVBoxLayout  # add cards here, then ``addStretch()``
    subtitle: QLabel
    actions: QHBoxLayout  # right side of the header row


def init_page(page: QWidget, title: str, subtitle: str = "") -> PageScaffold:
    """Give ``page`` the standard look: scrollable body, title + muted subtitle header."""
    page.setObjectName("Page")
    page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    page.setStyleSheet(input_stylesheet())
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea(page)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    content = QVBoxLayout(body)
    content.setContentsMargins(28, 24, 28, 28)
    content.setSpacing(16)

    header = QHBoxLayout()
    header.setSpacing(12)
    titles = QVBoxLayout()
    titles.setSpacing(2)
    title_label = QLabel(title)
    title_label.setObjectName("PageTitle")
    subtitle_label = muted_label(subtitle)
    subtitle_label.setWordWrap(True)
    titles.addWidget(title_label)
    titles.addWidget(subtitle_label)
    header.addLayout(titles, 1)
    actions = QHBoxLayout()
    actions.setSpacing(8)
    header.addLayout(actions)
    content.addLayout(header)

    scroll.setWidget(body)
    outer.addWidget(scroll)
    return PageScaffold(content=content, subtitle=subtitle_label, actions=actions)


def hint_label(text: str = "") -> QLabel:
    """Muted, word-wrapped explanatory text (never forces the page wider)."""
    label = muted_label(text)
    label.setWordWrap(True)
    return label


_GLYPHS: dict[str, list[tuple[float, float]]] = {
    "down": [(6, 9), (12, 15), (18, 9)],
    "up": [(6, 15), (12, 9), (18, 15)],
    "check": [(6, 12.5), (10.5, 17), (18.5, 7.5)],
}


def _glyph(name: str, color: str) -> str:
    """Path of a small cached PNG glyph (QSS can only draw sub-controls from images)."""
    folder = Path(tempfile.gettempdir()) / "ByteLog-ui"
    path = folder / f"{name}-{color.lstrip('#').lower()}.png"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        image = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if name == "dot":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(12, 12), 5, 5)
        else:
            pen = QPen(QColor(color), 2.6 if name != "check" else 3.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline([QPointF(x, y) for x, y in _GLYPHS[name]])
        painter.end()
        image.save(str(path))
    return path.as_posix()


def input_stylesheet() -> str:
    """Inputs under the Fusion style + app QSS: arrows, check boxes, radios, lists.

    The app stylesheet gives inputs a border, which makes Fusion draw broken native
    sub-controls and keeps light system colours in dark mode; this replaces them with
    flat token-coloured versions.
    """
    t = theme.tokens()
    try:
        down, up = _glyph("down", t.muted), _glyph("up", t.muted)
        check, dot = _glyph("check", "#FFFFFF"), _glyph("dot", "#FFFFFF")
    except OSError:
        return ""
    return f"""
    QCheckBox::indicator, QRadioButton::indicator, QListWidget::indicator {{
        width: 14px; height: 14px; border: 1px solid {t.muted}; background: {t.card}; }}
    QCheckBox::indicator, QListWidget::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:checked, QListWidget::indicator:checked {{
        background: {t.download}; border-color: {t.download}; image: url("{check}"); }}
    QRadioButton::indicator:checked {{ background: {t.download}; border-color: {t.download};
        image: url("{dot}"); }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        border-color: {t.border}; }}
    QListWidget {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 8px;
        padding: 4px; outline: none; }}
    QListWidget::item {{ padding: 3px 2px; color: {t.text}; }}
    QListWidget::item:selected, QListWidget::item:hover {{ background: {t.track};
        color: {t.text}; }}
    QSpinBox:disabled, QDoubleSpinBox:disabled, QDateEdit:disabled, QComboBox:disabled {{
        background: {t.page}; color: {t.muted}; }}
    QComboBox::drop-down, QDateEdit::drop-down {{ subcontrol-origin: padding;
        subcontrol-position: center right; width: 22px; border: none; }}
    QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url("{down}");
        width: 12px; height: 12px; }}
    QSpinBox, QDoubleSpinBox {{ padding-right: 24px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border;
        subcontrol-position: top right; width: 22px; border: none; background: transparent; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border;
        subcontrol-position: bottom right; width: 22px; border: none;
        background: transparent; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{up}");
        width: 10px; height: 10px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{down}");
        width: 10px; height: 10px; }}
    QComboBox QAbstractItemView {{ background: {t.card}; color: {t.text};
        border: 1px solid {t.border}; selection-background-color: {t.track};
        selection-color: {t.text}; outline: none; }}
    """


def pad_spin(spin: QAbstractSpinBox) -> QAbstractSpinBox:
    """Inset spin box text to match line edits (QSS padding does not reach it)."""
    spin.lineEdit().setTextMargins(5, 0, 0, 0)
    return spin


def mono_font(point_delta: float = 0.0) -> QFont:
    font = QFont(theme.MONO)
    if point_delta:
        font.setPointSizeF(max(6.0, font.pointSizeF() + point_delta))
    return font


def style_table(table: QTableView, stretch_column: int = 0, row_height: int = 34) -> None:
    """Flat, borderless-grid table that fits the card look."""
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().setDefaultSectionSize(row_height)


def fit_table_height(table: QTableView, max_rows: int | None = None) -> None:
    """Size a table to show all its rows (up to ``max_rows``) without its own scrollbar."""
    model = table.model()
    rows = model.rowCount() if model is not None else 0
    shown = rows if max_rows is None else min(rows, max_rows)
    height = table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth()
    height += sum(table.rowHeight(r) for r in range(shown)) if rows else 0
    if rows and table.horizontalScrollBar().isVisible():
        height += table.horizontalScrollBar().sizeHint().height()
    table.setFixedHeight(height + 2)


def set_tone(label: QLabel, tone: str | None) -> None:
    """Colour a label with a theme token name (``ok``, ``warn``, ``danger``, ``muted``)."""
    label.setProperty("tone", tone or "")
    apply_tone(label)


def apply_tone(label: QLabel) -> None:
    tone = label.property("tone")
    color = getattr(theme.tokens(), tone, None) if tone else None
    label.setStyleSheet(f"color: {color};" if color else "")


def restyle_tones(root: QWidget) -> None:
    """Re-apply token colours after a theme switch."""
    for label in root.findChildren(QLabel):
        if label.property("tone"):
            apply_tone(label)


def restyle_page(page: QWidget) -> None:
    """Theme switch: new arrow colours and tone colours."""
    page.setStyleSheet(input_stylesheet())
    restyle_tones(page)


def fmt_day(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def fmt_local_ts(ts: int | None, tz: tzinfo | None = None) -> str:
    if not ts:
        return "—"
    local = timeutil.to_local(ts, tz or timeutil.local_zone())
    return f"{fmt_day(local.date())}, {local:%H:%M}"


def fmt_short_ts(ts: int | None, tz: tzinfo, today: date) -> str:
    """``Today, 14:05`` / ``Yesterday, 09:10`` / ``3 Oct 2026`` for compact table cells."""
    if not ts:
        return "—"
    local = timeutil.to_local(ts, tz)
    days = (today - local.date()).days
    if days == 0:
        return f"Today, {local:%H:%M}"
    if days == 1:
        return f"Yesterday, {local:%H:%M}"
    return fmt_day(local.date())


def today_local(tz: tzinfo | None = None) -> date:
    return timeutil.local_date(timeutil.now_utc_s(), tz or timeutil.local_zone())


def db_file(conn: sqlite3.Connection) -> Path | None:
    """File behind ``conn`` (None for in-memory databases)."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def ago(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"
