"""Main window: sidebar on the left, stacked pages on the right."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from bytelog import APP_NAME
from bytelog.ui import theme
from bytelog.ui.context import AppContext
from bytelog.ui.icons import app_icon
from bytelog.ui.sidebar import NAV_ITEMS, Sidebar
from bytelog.ui.widgets import muted_label

log = logging.getLogger(__name__)

REFRESH_MS = 10_000

# page key -> (module, class name)
PAGES: dict[str, tuple[str, str]] = {
    "overview": ("bytelog.ui.pages.overview", "OverviewPage"),
    "reports": ("bytelog.ui.pages.reports", "ReportsPage"),
    "networks": ("bytelog.ui.pages.networks", "NetworksPage"),
    "plans": ("bytelog.ui.pages.plans", "PlansPage"),
    "settings": ("bytelog.ui.pages.settings", "SettingsPage"),
}


class PlaceholderPage(QWidget):
    """Shown when a page module is missing or fails to build."""

    def __init__(self, title: str, reason: str) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(4)
        heading = QLabel(title)
        heading.setObjectName("PageTitle")
        layout.addWidget(heading)
        layout.addWidget(muted_label(reason))
        layout.addStretch(1)

    def refresh(self) -> None:
        pass


def build_page(key: str, ctx: AppContext) -> QWidget:
    module_name, class_name = PAGES[key]
    title = dict(NAV_ITEMS)[key]
    try:
        module = importlib.import_module(module_name)
        page_class: Callable[[AppContext], QWidget] = getattr(module, class_name)
        return page_class(ctx)
    except Exception:
        log.exception("could not build page %s", key)
        return PlaceholderPage(title, "This page is not available yet.")


class MainWindow(QMainWindow):
    hidden_to_tray = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.quitting = False
        self.close_to_tray = True
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(720, 560)
        self.resize(1280, 820)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.sidebar = Sidebar(ctx)
        self.stack = QStackedWidget()
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.pages: dict[str, QWidget] = {}
        for key in PAGES:
            page = build_page(key, ctx)
            self.pages[key] = page
            self.stack.addWidget(page)
        self.current = ""

        self.sidebar.page_selected.connect(self.show_page)
        ctx.navigate.connect(self.show_page)
        ctx.theme_changed.connect(self.apply_theme)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self._tick)
        self.show_page("overview")

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            log.warning("unknown page %r", key)
            return
        self.current = key
        self.sidebar.set_current(key)
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        self.refresh_current()

    def current_page(self) -> QWidget:
        return self.pages[self.current]

    def refresh_current(self) -> None:
        page = self.current_page()
        refresh = getattr(page, "refresh", None)
        if refresh is None:
            return
        try:
            refresh()
        except Exception:
            log.exception("refresh of page %s failed", self.current)

    def _tick(self) -> None:
        # Only the current page needs fresh data; other pages refresh when shown. (Emitting
        # data_changed here too made a visible Reports page rebuild twice per tick.)
        if self.isVisible() and not self.isMinimized():
            self.refresh_current()

    def apply_theme(self, name: str | None = None) -> None:
        if name:
            theme.set_theme(name)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(theme.stylesheet())
        self.sidebar.apply_theme()
        for page in self.pages.values():
            hook = getattr(page, "apply_theme", None)
            if hook is not None:
                try:
                    hook()
                except Exception:
                    log.exception("theme update failed for %s", type(page).__name__)
        self.refresh_current()

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.timer.start()
        if self.current:
            self.refresh_current()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt API
        super().hideEvent(event)
        self.timer.stop()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.quitting or not self.close_to_tray:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.hidden_to_tray.emit()
