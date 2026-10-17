"""Dark navigation sidebar with the collector status panel."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from bytelog import APP_NAME
from bytelog.storage import settings
from bytelog.storage.models import CollectorStatus
from bytelog.timeutil import now_utc_s
from bytelog.ui import theme
from bytelog.ui.context import AppContext
from bytelog.ui.icons import logo_pixmap, nav_icon
from bytelog.ui.widgets import Dot, primary_button, repolish

log = logging.getLogger(__name__)

SIDEBAR_WIDTH = 220
STATUS_REFRESH_MS = 3000
BEAT_STALL_S = 60.0  # minimum time the beat may stay unchanged (dashboard monotonic clock)
START_TIMEOUT_S = 20.0

NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("reports", "Reports"),
    ("networks", "Networks"),
    ("plans", "Plans and alerts"),
    ("settings", "Settings"),
)


@dataclass(frozen=True)
class StatusView:
    level: str  # running | paused | starting | stopped
    title: str
    detail: str
    show_start: bool


def read_status(conn: sqlite3.Connection) -> CollectorStatus:
    try:
        from bytelog.storage.repo import read_collector_status
    except ImportError:
        row = conn.execute(
            "SELECT pid, state, started_utc, heartbeat_utc, beat, app_version, last_error "
            "FROM collector_status WHERE id=1"
        ).fetchone()
        if row is None:
            return CollectorStatus(None, "stopped", None, None, 0, None, None)
        return CollectorStatus(
            row["pid"],
            row["state"],
            row["started_utc"],
            row["heartbeat_utc"],
            int(row["beat"] or 0),
            row["app_version"],
            row["last_error"],
        )
    return read_collector_status(conn)


def beat_stall_limit_s(interval_s: int, poll_s: float) -> float:
    """How long the beat may look unchanged before the collector counts as stuck.

    Matches the heartbeat-age rule (``max(60, 3 * interval)``) plus one poll period: a
    poller only sees a change at its next poll, so a healthy collector beating every
    interval can look unchanged for up to ``interval + poll_s``.
    """
    return max(BEAT_STALL_S, 3 * interval_s) + poll_s


def ago_text(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 2:
        return "just now"
    if seconds < 120:
        return f"{seconds} seconds ago"
    if seconds < 7200:
        return f"{seconds // 60} minutes ago"
    if seconds < 172800:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


def describe_status(
    status: CollectorStatus,
    now_utc: int,
    interval_s: int,
    beat_stalled: bool,
    starting: bool,
) -> StatusView:
    """Pure mapping from the heartbeat row to what the panel shows."""
    age = None if status.heartbeat_utc is None else now_utc - status.heartbeat_utc
    stale = (
        status.state == "stopped" or age is None or age > max(60, 3 * interval_s) or beat_stalled
    )
    last = "No samples yet" if age is None else f"Last sample {ago_text(age)}"
    if stale:
        if starting:
            return StatusView("starting", "Starting…", "Waiting for the collector", False)
        return StatusView("stopped", "Collector stopped", last, True)
    if status.state == "paused":
        return StatusView("paused", "Collection paused", f"Last heartbeat {ago_text(age)}", False)
    return StatusView("running", "Collector running", last, False)


class StatusPanel(QFrame):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("StatusPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._last_beat: int | None = None
        self._beat_changed_at = time.monotonic()
        self._start_clicked_at: float | None = None
        self._start_beat: int | None = None
        self.view: StatusView | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.dot = Dot(theme.tokens().ok)
        self.title = QLabel("Collector")
        self.title.setObjectName("StatusTitle")
        self.title.setStyleSheet("font-weight: 500;")
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.title, 1)
        layout.addLayout(row)
        self.detail = QLabel("")
        self.detail.setProperty("muted", True)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.start_button = primary_button("Start collector")
        self.start_button.clicked.connect(self._on_start)
        layout.addSpacing(6)
        layout.addWidget(self.start_button)
        self.start_button.hide()

        self.timer = QTimer(self)
        self.timer.setInterval(STATUS_REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    # Poll only while visible: the dashboard spends most of its life hidden in the tray.
    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self.timer.isActive():
            self.timer.start()
            self.refresh()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt API
        super().hideEvent(event)
        self.timer.stop()

    def refresh(self) -> None:
        try:
            status = read_status(self.ctx.conn)
            interval = settings.sample_interval_s(self.ctx.conn)
        except sqlite3.Error:
            log.exception("could not read collector status")
            return
        now_mono = time.monotonic()
        if status.beat != self._last_beat:
            self._last_beat = status.beat
            self._beat_changed_at = now_mono
        limit = beat_stall_limit_s(interval, STATUS_REFRESH_MS / 1000)
        stalled = now_mono - self._beat_changed_at > limit
        starting = False
        if self._start_clicked_at is not None:
            moved = status.beat != self._start_beat
            if moved or now_mono - self._start_clicked_at > START_TIMEOUT_S:
                self._start_clicked_at = None
                if moved:
                    stalled = False
            else:
                starting = True
        self._show(describe_status(status, now_utc_s(), interval, stalled, starting))

    def _show(self, view: StatusView) -> None:
        if view == self.view:
            return
        self.view = view
        t = theme.tokens()
        color = {"running": t.ok, "paused": t.warn, "starting": t.warn}.get(view.level, t.danger)
        self.dot.set_color(color)
        self.title.setText(view.title)
        self.detail.setText(view.detail)
        self.start_button.setVisible(view.show_start)
        self.start_button.setEnabled(view.show_start)

    def _on_start(self) -> None:
        self._start_clicked_at = time.monotonic()
        self._start_beat = self._last_beat
        self.start_button.setEnabled(False)
        try:
            self.ctx.platform.launch_collector()
        except Exception:
            log.exception("could not launch the collector")
            self._start_clicked_at = None
        self.view = None
        self.refresh()

    def apply_theme(self) -> None:
        self.view = None
        self.refresh()


class Sidebar(QFrame):
    page_selected = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(SIDEBAR_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(2)

        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 0, 0)
        brand.setSpacing(10)
        self.logo = QLabel()
        self.logo.setFixedSize(QSize(24, 24))
        self.logo.setPixmap(logo_pixmap(24))
        name = QLabel(APP_NAME)
        name.setObjectName("AppName")
        brand.addWidget(self.logo)
        brand.addWidget(name, 1)
        layout.addLayout(brand)
        layout.addSpacing(24)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in NAV_ITEMS:
            button = QPushButton(text)
            button.setObjectName("NavItem")
            button.setIcon(nav_icon(key))
            button.setIconSize(QSize(16, 16))
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=key: self.page_selected.emit(k))
            self.group.addButton(button)
            self.buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)
        self.status = StatusPanel(ctx)
        layout.addWidget(self.status)

    def set_current(self, key: str) -> None:
        button = self.buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def apply_theme(self) -> None:
        self.logo.setPixmap(logo_pixmap(24))
        for key, button in self.buttons.items():
            button.setIcon(nav_icon(key))
            repolish(button)
        self.status.apply_theme()
