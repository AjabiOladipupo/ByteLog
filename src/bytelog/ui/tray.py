"""System tray icon: today's total in the tooltip, pause/resume, open and quit."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from bytelog import APP_NAME
from bytelog.storage import periods, settings
from bytelog.ui.context import AppContext
from bytelog.ui.icons import tray_icon
from bytelog.units import format_bytes

log = logging.getLogger(__name__)

TOOLTIP_REFRESH_MS = 10_000
PAUSE_KEY = "collection_paused"


def tooltip_text(today_bytes: int, paused: bool) -> str:
    text = f"{APP_NAME} — Today: {format_bytes(today_bytes)}"
    return f"{text} (paused)" if paused else text


class Tray(QObject):
    """Owns the QSystemTrayIcon. ``open_dashboard`` is called for Open / double-click."""

    def __init__(self, ctx: AppContext, open_dashboard: Callable[[], None]) -> None:
        super().__init__()
        self.ctx = ctx
        self.open_dashboard = open_dashboard
        self._paused: bool | None = None

        self.icon = QSystemTrayIcon(tray_icon(), self)
        self.menu = QMenu()
        self.open_action = QAction("Open dashboard", self.menu)
        self.open_action.triggered.connect(self.open_dashboard)
        self.pause_action = QAction("Pause collection", self.menu)
        self.pause_action.triggered.connect(self.toggle_pause)
        self.quit_action = QAction("Quit dashboard", self.menu)
        self.quit_action.triggered.connect(self.quit)
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.pause_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.menu.aboutToShow.connect(self.refresh)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)

        self.timer = QTimer(self)
        self.timer.setInterval(TOOLTIP_REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        ctx.settings_changed.connect(self._on_setting_changed)
        self.refresh()

    def show(self) -> None:
        self.icon.show()

    def is_paused(self) -> bool:
        return settings.get_bool(self.ctx.conn, PAUSE_KEY)

    def refresh(self) -> None:
        try:
            paused = self.is_paused()
            today = periods.today_total(self.ctx.conn)
        except sqlite3.Error:
            log.exception("tray refresh failed")
            return
        self.icon.setToolTip(tooltip_text(today, paused))
        if paused != self._paused:
            self._paused = paused
            self.pause_action.setText("Resume collection" if paused else "Pause collection")
            self.icon.setIcon(tray_icon(paused))

    def toggle_pause(self) -> None:
        try:
            paused = not self.is_paused()
            settings.set_value(self.ctx.conn, PAUSE_KEY, paused)
        except sqlite3.Error:
            log.exception("could not change %s", PAUSE_KEY)
            return
        log.info("collection %s from the tray", "paused" if paused else "resumed")
        self.ctx.settings_changed.emit(PAUSE_KEY)
        self.refresh()

    def _on_setting_changed(self, key: str) -> None:
        if key == PAUSE_KEY:
            self.refresh()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.open_dashboard()

    def show_message(self, title: str, message: str) -> None:
        self.icon.showMessage(title, message, tray_icon(), 6000)

    def quit(self) -> None:
        """Quit the dashboard only; the collector keeps running."""
        self.icon.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()
