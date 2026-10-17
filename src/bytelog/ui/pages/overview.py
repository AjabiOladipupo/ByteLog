"""Overview: period selector, summary cards, usage chart, plan and per-network cards."""

from __future__ import annotations

import html
import importlib.util
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from bytelog.storage import periods, plans, settings
from bytelog.storage.db import connect
from bytelog.storage.models import NetworkUsage, Period, PeriodSummary, PlanStatus
from bytelog.ui import theme
from bytelog.ui.charts.stacked_bar import ChartLegend, StackedBarChart
from bytelog.ui.context import AppContext
from bytelog.ui.fonts import mono_family
from bytelog.ui.widgets import Card, Dot, ProgressBar, muted_label, primary_button
from bytelog.units import format_bytes, percent, split_value_unit

log = logging.getLogger(__name__)

PERIOD_SETTING = "overview_period"
PERIOD_CHOICES: tuple[tuple[Period, str], ...] = (
    (Period.TODAY, "Today"),
    (Period.WEEK, "This week"),
    (Period.MONTH, "This month"),
    (Period.ALL, "All time"),
)
DEFAULT_PERIOD = Period.MONTH
RIGHT_COLUMN_WIDTH = 320
CHART_MIN_HEIGHT = 280
CHART_MIN_HEIGHT_STACKED = 380
STACK_BELOW_PX = 900  # window width under which the right column always moves below
CHART_MIN_WIDTH_SIDE_BY_SIDE = 460  # ...and also whenever the chart would get less room
PAGE_MARGINS_X = 28 + 28
MAIN_ROW_SPACING = 16
CARDS_TWO_COLUMNS_BELOW_PX = 760  # page width under which the cards form a 2 x 2 grid
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


_NUMBER = re.compile(r"\d(?:[\d.,]*\d)?(?:\s?(?:%|GB|MB|KB)(?![A-Za-z]))?")


def mono_numbers(text: str) -> str:
    """Rich text with the numbers (and their units) in the mono face, the words in sans."""
    span = f"<span style=\"font-family: '{mono_family()}'\">"
    parts: list[str] = []
    pos = 0
    for match in _NUMBER.finditer(text):
        parts.append(html.escape(text[pos : match.start()]))
        parts.append(f"{span}{html.escape(match.group())}</span>")
        pos = match.end()
    parts.append(html.escape(text[pos:]))
    return "".join(parts)


def _rich(label: QLabel) -> QLabel:
    label.setTextFormat(Qt.TextFormat.RichText)
    return label


def short_date(day: date) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}"


def cycle_label(status: PlanStatus) -> str:
    if status.cycle is None:
        anchor = status.plan.anchor_date
        return f"Starts {short_date(anchor)}" if anchor else "Not started"
    return f"{short_date(status.cycle.start)} – {short_date(status.cycle.last_day)}"


def days_left_label(status: PlanStatus) -> str:
    if status.cycle is None:
        return "Cycle has not started yet"
    days = f"{status.days_left} day{'s' if status.days_left != 1 else ''} left"
    if status.over_by > 0:
        return f"{days} · over by {format_bytes(status.over_by)}"
    return f"{days} · {format_bytes(status.daily_allowance)}/day allowance"


def database_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row[2])


def scrollbar_qss() -> str:
    """Thin rounded vertical scrollbar in the page colours (Fusion's default is heavy)."""
    t = theme.tokens()
    return (
        f"QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}"
        f"QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 3px;"
        f" min-height: 32px; }}"
        f"QScrollBar::handle:vertical:hover {{ background: {t.muted}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
    )


def tint(color: str, alpha: int) -> str:
    """``#RRGGBB`` + alpha (0-255) as a QSS ``rgba()``."""
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


# -- export worker -------------------------------------------------------------------------


class _ExportSignals(QObject):
    done = Signal(str, str)  # kind, path
    failed = Signal(str, str)  # kind, error message


class ExportJob(QRunnable):
    """Runs a report export on its own SQLite connection in the global thread pool."""

    def __init__(
        self,
        kind: str,
        db_file: str,
        run: Callable[[sqlite3.Connection, Path], object],
        path: Path,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.db_file = db_file
        self.run_export = run
        self.path = path
        self.signals = _ExportSignals()

    def run(self) -> None:
        try:
            conn = connect(self.db_file)
            try:
                self.run_export(conn, self.path)
            finally:
                conn.close()
        except Exception as exc:
            log.exception("export to %s failed", self.path)
            self.signals.failed.emit(self.kind, str(exc))
        else:
            self.signals.done.emit(self.kind, str(self.path))


# -- small building blocks -----------------------------------------------------------------


class Segmented(QFrame):
    changed = Signal(str)

    def __init__(
        self, choices: tuple[tuple[Period, str], ...], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Segmented")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in choices:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=key.value: self.changed.emit(k))
            self.group.addButton(button)
            self.buttons[key.value] = button
            layout.addWidget(button)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_current(self, key: str) -> None:
        self.buttons[key].setChecked(True)


class StatCard(Card):
    """Small caption, big mono number with a smaller unit, muted detail line."""

    def __init__(self, caption: str, dot_role: str | None = None) -> None:
        super().__init__()
        self.body.setSpacing(6)
        self._dot_role = dot_role
        head = QHBoxLayout()
        head.setSpacing(7)
        self.dot: Dot | None = None
        if dot_role:
            self.dot = Dot(getattr(theme.tokens(), dot_role), 8)
            head.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.caption = muted_label(caption)
        head.addWidget(self.caption, 1)
        self.body.addLayout(head)
        number = QHBoxLayout()
        number.setSpacing(5)
        self.value = QLabel("0")
        self.value.setObjectName("BigNumber")
        self.unit = QLabel("MB")
        self.unit.setObjectName("BigUnit")
        number.addWidget(self.value, 0, Qt.AlignmentFlag.AlignBaseline)
        number.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBaseline)
        number.addStretch(1)
        self.body.addLayout(number)
        self.detail = _rich(muted_label(""))
        self.detail.setStyleSheet("font-size: 12px;")
        self.body.addWidget(self.detail)
        self.setMinimumWidth(150)

    def set_values(self, n: int, detail: str, caption: str | None = None) -> None:
        value, unit = split_value_unit(n)
        self.value.setText(value)
        self.unit.setText(unit)
        self.detail.setText(mono_numbers(detail))
        if caption is not None:
            self.caption.setText(caption)

    def apply_theme(self) -> None:
        if self.dot is not None and self._dot_role:
            self.dot.set_color(getattr(theme.tokens(), self._dot_role))


class Badge(QLabel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hide()
        self._danger = False

    def show_badge(self, text: str, danger: bool) -> None:
        self._danger = danger
        self.setText(text)
        self.apply_theme()
        self.show()

    def apply_theme(self) -> None:
        t = theme.tokens()
        color = t.danger if self._danger else t.plan
        self.setStyleSheet(
            f"background: {tint(color, 34)}; color: {color}; border-radius: 9px;"
            "padding: 2px 9px; font-size: 11px; font-weight: 600;"
        )


class PlanCard(Card):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        head = QHBoxLayout()
        title = QLabel("Data plan")
        title.setObjectName("CardTitle")
        head.addWidget(title, 1)
        self.badge = Badge()
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(head)

        # Filled state.
        self.filled = QWidget()
        filled = QVBoxLayout(self.filled)
        filled.setContentsMargins(0, 2, 0, 0)
        filled.setSpacing(6)
        self.name = QLabel("")
        self.name.setStyleSheet("font-size: 14px; font-weight: 500;")
        filled.addWidget(self.name)
        usage = QHBoxLayout()
        usage.setSpacing(6)
        self.used = QLabel("")
        self.used.setObjectName("BigNumber")
        self.used.setStyleSheet("font-size: 22px;")
        self.used_unit = QLabel("")
        self.used_unit.setObjectName("BigUnit")
        self.cap = _rich(muted_label(""))
        self.cap.setStyleSheet("font-size: 12px;")
        usage.addWidget(self.used, 0, Qt.AlignmentFlag.AlignBaseline)
        usage.addWidget(self.used_unit, 0, Qt.AlignmentFlag.AlignBaseline)
        usage.addSpacing(4)
        usage.addWidget(self.cap, 0, Qt.AlignmentFlag.AlignBaseline)
        usage.addStretch(1)
        filled.addLayout(usage)
        filled.addSpacing(2)
        self.bar = ProgressBar(theme.tokens().plan, 8)
        filled.addWidget(self.bar)
        dates = QHBoxLayout()
        self.cycle = _rich(muted_label(""))
        self.cycle.setStyleSheet("font-size: 12px;")
        self.percent = _rich(muted_label(""))
        self.percent.setStyleSheet("font-size: 12px;")
        dates.addWidget(self.cycle, 1)
        dates.addWidget(self.percent)
        filled.addLayout(dates)
        self.days = _rich(QLabel(""))
        self.days.setStyleSheet("font-size: 12px;")
        filled.addWidget(self.days)
        self.body.addWidget(self.filled)

        # Empty state.
        self.empty = QWidget()
        empty = QVBoxLayout(self.empty)
        empty.setContentsMargins(0, 2, 0, 0)
        empty.setSpacing(10)
        message = QLabel("No data plan yet")
        message.setStyleSheet("font-size: 14px; font-weight: 500;")
        hint = muted_label("Add your MiFi or hotspot plan to track its cap and get alerts.")
        hint.setWordWrap(True)
        button = primary_button("Add a plan")
        button.clicked.connect(lambda: ctx.navigate.emit("plans"))
        empty.addWidget(message)
        empty.addWidget(hint)
        empty.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        self.body.addWidget(self.empty)
        self.filled.hide()
        self.status: PlanStatus | None = None

    def set_status(self, status: PlanStatus | None) -> None:
        self.status = status
        self.filled.setVisible(status is not None)
        self.empty.setVisible(status is None)
        if status is None:
            self.badge.hide()
            return
        t = theme.tokens()
        over = status.percent >= 100
        self.name.setText(status.plan.name)
        value, unit = split_value_unit(status.used)
        self.used.setText(value)
        self.used_unit.setText(unit)
        self.cap.setText(mono_numbers(f"of {format_bytes(status.plan.cap_bytes)}"))
        self.bar.set_color(t.danger if over else t.plan)
        self.bar.set_value(status.percent / 100)
        self.cycle.setText(mono_numbers(cycle_label(status)))
        self.percent.setText(mono_numbers(f"{status.percent:.0f}% used"))
        self.days.setText(mono_numbers(days_left_label(status)))
        if status.crossed:
            self.badge.show_badge(f"{status.crossed[-1]}% alert", over)
        else:
            self.badge.hide()

    def apply_theme(self) -> None:
        self.badge.apply_theme()
        self.set_status(self.status)


class ElidedLabel(QLabel):
    """Single-line label that elides with "…" to its current width (tooltip = full text)."""

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        if text != self._full:
            self._full = text
            self.setToolTip(text)
            self._elide()

    def _elide(self) -> None:
        width = max(self.width(), 40)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, width))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._elide()


class NetworkRow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.label = ElidedLabel()
        self.amount = QLabel("")
        self.amount.setProperty("mono", True)
        top.addWidget(self.label, 1)
        top.addWidget(self.amount)
        layout.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.bar = ProgressBar(None, 4)
        self.share = muted_label("")
        self.share.setProperty("mono", True)
        self.share.setStyleSheet("font-size: 11px;")
        self.share.setFixedWidth(34)
        self.share.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bottom.addWidget(self.bar, 1, Qt.AlignmentFlag.AlignVCenter)
        bottom.addWidget(self.share)
        layout.addLayout(bottom)

    def set_usage(self, usage: NetworkUsage, total: int) -> None:
        self.label.set_full_text(usage.label)
        self.amount.setText(format_bytes(usage.total))
        share = percent(usage.total, total)
        self.bar.set_value(share / 100)
        self.share.setText(f"{share:.0f}%")


class NetworksCard(Card):
    def __init__(self) -> None:
        super().__init__("By network")
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(14)
        self.body.addSpacing(4)
        self.body.addLayout(self.rows_box)
        self.empty = muted_label("No usage recorded in this period.")
        self.empty.setWordWrap(True)
        self.body.addWidget(self.empty)
        self.rows: list[NetworkRow] = []

    def set_usage(self, usages: list[NetworkUsage]) -> None:
        usages = [u for u in usages if u.total > 0]
        while len(self.rows) < len(usages):
            row = NetworkRow()
            self.rows.append(row)
            self.rows_box.addWidget(row)
        total = sum(u.total for u in usages)
        for i, row in enumerate(self.rows):
            if i < len(usages):
                row.set_usage(usages[i], total)
                row.show()
            else:
                row.hide()
        self.empty.setVisible(not usages)

    def apply_theme(self) -> None:
        for row in self.rows:
            row.bar.update()


# -- the page ------------------------------------------------------------------------------


class OverviewPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setObjectName("Page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.summary: PeriodSummary | None = None
        self._stacked = False
        self._pool = QThreadPool.globalInstance()
        self._jobs: dict[str, ExportJob] = {}

        saved = settings.get(ctx.conn, PERIOD_SETTING)
        self.period = saved if saved in {p.value for p, _ in PERIOD_CHOICES} else DEFAULT_PERIOD

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(scrollbar_qss())
        outer.addWidget(self.scroll)
        body = QWidget()
        self.scroll.setWidget(body)
        content = QVBoxLayout(body)
        content.setContentsMargins(PAGE_MARGINS_X // 2, 24, PAGE_MARGINS_X // 2, 28)
        content.setSpacing(16)

        content.addLayout(self._build_header())
        self.segmented = Segmented(PERIOD_CHOICES)
        self.segmented.set_current(self.period)
        self.segmented.changed.connect(self.set_period)
        content.addWidget(self.segmented, 0, Qt.AlignmentFlag.AlignLeft)

        self.cards_grid = QGridLayout()
        self.cards_grid.setHorizontalSpacing(16)
        self.cards_grid.setVerticalSpacing(16)
        self.total_card = StatCard("Total used")
        self.down_card = StatCard("Downloaded", "download")
        self.up_card = StatCard("Uploaded", "upload")
        self.avg_card = StatCard("Daily average")
        self.stat_cards = [self.total_card, self.down_card, self.up_card, self.avg_card]
        self._cards_columns = 0
        self._layout_cards(4)
        content.addLayout(self.cards_grid)

        self.main_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.main_row.setSpacing(MAIN_ROW_SPACING)
        self.chart_card = self._build_chart_card()
        self.main_row.addWidget(self.chart_card, 1)
        self.right = QWidget()
        right = QVBoxLayout(self.right)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(16)
        self.plan_card = PlanCard(ctx)
        self.networks_card = NetworksCard()
        right.addWidget(self.plan_card)
        right.addWidget(self.networks_card)
        right.addStretch(1)
        self.right.setFixedWidth(RIGHT_COLUMN_WIDTH)
        self.main_row.addWidget(self.right, 0)
        content.addLayout(self.main_row, 1)
        content.addStretch(0)

        ctx.plans_changed.connect(self.refresh)
        ctx.networks_changed.connect(self.refresh)
        ctx.settings_changed.connect(self._on_setting_changed)

    # -- construction -------------------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Data usage")
        title.setObjectName("PageTitle")
        self.subtitle = muted_label("")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.csv_button = QPushButton("Export CSV")
        self.pdf_button = primary_button("PDF report")
        self.csv_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.csv_button.clicked.connect(lambda: self._export("csv"))
        self.pdf_button.clicked.connect(lambda: self._export("pdf"))
        for button, module in (
            (self.csv_button, "bytelog.reports.csv_export"),
            (self.pdf_button, "bytelog.reports.pdf_report"),
        ):
            if importlib.util.find_spec(module) is None:
                button.setEnabled(False)
                button.setToolTip("Reports are not available in this build")
            header.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return header

    def _build_chart_card(self) -> Card:
        card = Card()
        card.body.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel("Usage")
        title.setObjectName("CardTitle")
        head.addWidget(title, 1)
        self.legend = ChartLegend()
        head.addWidget(self.legend)
        card.body.addLayout(head)
        self.chart = StackedBarChart()
        self.chart.setMinimumHeight(CHART_MIN_HEIGHT)
        card.body.addWidget(self.chart, 1)
        self.caption = muted_label("")
        self.caption.setStyleSheet("font-size: 12px;")
        card.body.addWidget(self.caption)
        return card

    def _layout_cards(self, columns: int) -> None:
        if columns == self._cards_columns:
            return
        self._cards_columns = columns
        for card in self.stat_cards:
            self.cards_grid.removeWidget(card)
        for i, card in enumerate(self.stat_cards):
            self.cards_grid.addWidget(card, i // columns, i % columns)
        for col in range(4):
            self.cards_grid.setColumnStretch(col, 1 if col < columns else 0)

    # -- behaviour ----------------------------------------------------------------------

    def set_period(self, key: str, persist: bool = True) -> None:
        key = Period(key).value
        self.segmented.set_current(key)
        if key != self.period:
            self.period = key
            if persist:
                try:
                    settings.set_value(self.ctx.conn, PERIOD_SETTING, key)
                except sqlite3.Error:
                    log.exception("could not save the overview period")
        self.refresh()

    def _on_setting_changed(self, key: str) -> None:
        if key in {"week_start", "collection_paused"}:
            self.refresh()

    def refresh(self) -> None:
        try:
            summary = periods.summarize(self.ctx.conn, Period(self.period))
            active = plans.list_plans(self.ctx.conn, active_only=True)
            status = plans.plan_status(self.ctx.conn, active[0]) if active else None
        except sqlite3.Error:
            log.exception("overview refresh failed")
            return
        self.summary = summary
        self._show_summary(summary)
        self.plan_card.set_status(status)

    def _show_summary(self, s: PeriodSummary) -> None:
        period = Period(s.period)
        self.subtitle.setText(
            periods.date_range_label(s.start, s.end, period if period is Period.ALL else None)
        )
        networks = len([u for u in s.per_network if u.total > 0])
        across = f"across {networks} network{'s' if networks != 1 else ''}"
        self.total_card.set_values(s.total, across if networks else "Download + upload")
        self.down_card.set_values(s.rx, f"{percent(s.rx, s.total):.0f}% of total")
        self.up_card.set_values(s.tx, f"{percent(s.tx, s.total):.0f}% of total")
        self.avg_card.set_values(s.average_bytes, s.average_detail, s.average_title)
        self.chart.set_buckets(s.buckets)
        tallest = self.chart.tallest()
        if tallest is None:
            self.caption.setText("No usage recorded in this period yet.")
        else:
            self.caption.setText(
                f"Tallest bar: {tallest.tooltip_title} · {format_bytes(tallest.total)}"
            )
        self.networks_card.set_usage(s.per_network)

    def apply_theme(self) -> None:
        self.scroll.setStyleSheet(scrollbar_qss())
        self.chart.apply_theme()
        self.legend.update()
        for card in self.stat_cards:
            card.apply_theme()
        self.plan_card.apply_theme()
        self.networks_card.apply_theme()

    # -- responsive layout --------------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        window = self.window()
        window_width = window.width() if window is not None else self.width()
        # Side by side, the chart gets what the fixed right column leaves; never squeeze it.
        chart_room = self.width() - PAGE_MARGINS_X - MAIN_ROW_SPACING - RIGHT_COLUMN_WIDTH
        self._set_stacked(
            window_width < STACK_BELOW_PX or chart_room < CHART_MIN_WIDTH_SIDE_BY_SIDE
        )
        # The scroll viewport has not been resized yet at this point; use our own width.
        self._layout_cards(2 if self.width() < CARDS_TWO_COLUMNS_BELOW_PX else 4)

    def _set_stacked(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        # Stacked, the page scrolls anyway, so give the chart a comfortable fixed floor.
        self.chart.setMinimumHeight(CHART_MIN_HEIGHT_STACKED if stacked else CHART_MIN_HEIGHT)
        if stacked:
            self.main_row.setDirection(QBoxLayout.Direction.TopToBottom)
            self.right.setMinimumWidth(0)
            self.right.setMaximumWidth(16777215)
        else:
            self.main_row.setDirection(QBoxLayout.Direction.LeftToRight)
            self.right.setFixedWidth(RIGHT_COLUMN_WIDTH)

    # -- exports ------------------------------------------------------------------------

    def _export(self, kind: str) -> None:
        s = self.summary
        if s is None:
            return
        suffix = "csv" if kind == "csv" else "pdf"
        default = Path.home() / "Documents" / f"bytelog-{s.start}-to-{s.end}.{suffix}"
        if not default.parent.is_dir():
            default = Path.home() / default.name
        filters = "CSV files (*.csv)" if kind == "csv" else "PDF files (*.pdf)"
        title = "Export CSV" if kind == "csv" else "Save PDF report"
        chosen, _ = QFileDialog.getSaveFileName(self, title, str(default), filters)
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() != f".{suffix}":
            path = path.with_suffix(f".{suffix}")
        start, end = s.start, s.end
        if kind == "csv":
            from bytelog.reports.csv_export import export_csv

            def run(conn: sqlite3.Connection, target: Path) -> object:
                return export_csv(conn, target, start, end)
        else:
            from bytelog.reports.pdf_report import export_pdf

            def run(conn: sqlite3.Connection, target: Path) -> object:
                return export_pdf(conn, target, start, end)

        button = self._export_button(kind)
        button.setEnabled(False)
        button.setText("Exporting…")
        job = ExportJob(kind, database_path(self.ctx.conn), run, path)
        job.setAutoDelete(False)
        self._jobs[kind] = job
        # Bound slots of this (GUI-thread) object, so delivery is queued to the GUI thread.
        job.signals.done.connect(self._on_export_done)
        job.signals.failed.connect(self._on_export_failed)
        self._pool.start(job)

    def _export_button(self, kind: str) -> QPushButton:
        return self.csv_button if kind == "csv" else self.pdf_button

    def _finish_export(self, kind: str) -> None:
        self._jobs.pop(kind, None)
        button = self._export_button(kind)
        button.setEnabled(True)
        button.setText("Export CSV" if kind == "csv" else "PDF report")

    @Slot(str, str)
    def _on_export_done(self, kind: str, path: str) -> None:
        self._finish_export(kind)
        self._export_done(Path(path))

    @Slot(str, str)
    def _on_export_failed(self, kind: str, message: str) -> None:
        self._finish_export(kind)
        self._export_failed(message)

    def _export_done(self, path: Path) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Export complete")
        box.setText(f"Saved {path.name}")
        box.setInformativeText(str(path.parent))
        open_button = box.addButton("Open", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_button:
            try:
                self.ctx.platform.open_path(path)
            except Exception:
                log.exception("could not open %s", path)

    def _export_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Export failed", f"The export could not be written.\n\n{message}")
