"""Dashboard entry point: ``python -m bytelog.ui`` / ``ByteLog.exe``."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from bytelog import APP_NAME, __version__
from bytelog.logging_setup import setup_logging
from bytelog.paths import is_frozen, set_data_dir_override
from bytelog.platform import get_platform
from bytelog.platform.base import Platform
from bytelog.storage import settings
from bytelog.storage.db import open_db
from bytelog.ui import theme
from bytelog.ui.context import AppContext
from bytelog.ui.fonts import load_fonts
from bytelog.ui.icons import app_icon

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "ByteLog.App"
SHOW_COMMAND = b"show"
CLOSE_HINT_KEY = "close_hint_shown"


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"ByteLog.Dashboard.{user}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bytelog", description=f"{APP_NAME} dashboard")
    parser.add_argument("--data-dir", help="use this data directory instead of the default")
    parser.add_argument("--tray", action="store_true", help="start hidden in the tray")
    parser.add_argument("--screenshot", metavar="PATH", help="render the window to a PNG, exit")
    parser.add_argument("--page", default="overview", help="page for --screenshot")
    parser.add_argument("--period", help="overview period for --screenshot")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    parser.add_argument("--theme", choices=("light", "dark"), help="override the theme")
    return parser.parse_args(argv)


def notify_running_instance(name: str) -> bool:
    """True if another dashboard answered (and was asked to show its window)."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(500):
        return False
    socket.write(QByteArray(SHOW_COMMAND))
    socket.flush()
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


class InstanceServer(QObject):
    show_requested = Signal()

    def __init__(self, name: str) -> None:
        super().__init__()
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(name):
            QLocalServer.removeServer(name)  # stale socket after a crash (non-Windows)
            if not self.server.listen(name):
                log.warning("single-instance server unavailable: %s", self.server.errorString())
        self.server.newConnection.connect(self._on_connection)

    def _on_connection(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        if bytes(socket.readAll().data()).strip() == SHOW_COMMAND:
            self.show_requested.emit()


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        log.debug("could not set AppUserModelID", exc_info=True)


def _make_platform() -> Platform:
    try:
        return get_platform()
    except Exception:
        # The dashboard can still show data without OS integration (Start, toasts...).
        log.exception("platform layer unavailable; using an inert stand-in")
        from bytelog.platform.fake import FakePlatform

        return FakePlatform()


def ensure_autostart(
    platform: Platform,
    wanted: bool,
    frozen: bool,
    on_done: Callable[[], None] | None = None,
) -> threading.Thread | None:
    """Register start-at-login in the background when the setting wants it and it is missing.

    ``start_at_login`` defaults to on, but only the Settings toggle registered the task, so a
    fresh install never started at login. Dev runs are skipped: the task would point at the
    development interpreter.
    """
    if not (wanted and frozen):
        return None

    def work() -> None:
        try:
            if not platform.autostart_enabled():
                platform.set_autostart(True)
                log.info("registered the collector to start at login")
        except Exception:
            log.exception("could not register start at login")
        if on_done is not None:
            on_done()  # runs on this worker thread

    thread = threading.Thread(target=work, name="ensure-autostart", daemon=True)
    thread.start()
    return thread


def take_screenshot(window: object, args: argparse.Namespace, app: QApplication) -> int:
    from bytelog.ui.main_window import MainWindow

    assert isinstance(window, MainWindow)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(args.width, args.height)
    window.show_page(args.page)
    if args.period and args.page == "overview":
        page = window.pages["overview"]
        set_period = getattr(page, "set_period", None)
        if set_period is not None:
            set_period(args.period, persist=False)
    window.show()
    for _ in range(5):
        app.processEvents()
    window.refresh_current()
    for _ in range(5):
        app.processEvents()
    target = Path(args.screenshot)
    target.parent.mkdir(parents=True, exist_ok=True)
    ok = window.grab().save(str(target), "PNG")
    log.info("screenshot %s -> %s", "saved" if ok else "FAILED", target)
    window.quitting = True
    window.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    set_data_dir_override(args.data_dir)
    setup_logging("dashboard")
    log.info("ByteLog dashboard %s starting", __version__)
    _set_windows_app_id()

    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    screenshot = bool(args.screenshot)
    instance: InstanceServer | None = None
    if not screenshot:
        name = server_name()
        if notify_running_instance(name):
            log.info("another dashboard is running; asked it to show its window")
            return 0
        instance = InstanceServer(name)

    load_fonts()
    conn = open_db()
    theme.set_theme(args.theme or settings.get(conn, "theme"))
    app.setStyleSheet(theme.stylesheet())

    from bytelog.ui.main_window import MainWindow

    ctx = AppContext(conn, _make_platform())
    window = MainWindow(ctx)
    if screenshot:
        code = take_screenshot(window, args, app)
        conn.close()
        return code

    from bytelog.ui.tray import Tray

    tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
    tray = Tray(ctx, window.bring_to_front)
    if tray_ok:
        tray.show()
    else:
        window.close_to_tray = False
        app.setQuitOnLastWindowClosed(True)

    def on_hidden_to_tray() -> None:
        if settings.get_bool(conn, CLOSE_HINT_KEY):
            return
        tray.show_message(
            f"{APP_NAME} is still running",
            "The dashboard is in the system tray. Use Quit dashboard in its menu to exit.",
        )
        try:
            settings.set_value(conn, CLOSE_HINT_KEY, True)
        except Exception:
            log.exception("could not save %s", CLOSE_HINT_KEY)

    # Settings may have read the task state before this registers it, so it re-reads after.
    settings_page = window.pages.get("settings")
    ensure_autostart(
        ctx.platform,
        settings.get_bool(conn, "start_at_login"),
        is_frozen(),
        getattr(settings_page, "reload_autostart_threadsafe", None),
    )
    window.hidden_to_tray.connect(on_hidden_to_tray)
    if instance is not None:
        instance.show_requested.connect(window.bring_to_front)

    def on_quit() -> None:
        window.quitting = True
        tray.icon.hide()

    app.aboutToQuit.connect(on_quit)
    if not (args.tray and tray_ok):
        # Defer so the first paint happens inside the event loop.
        QTimer.singleShot(0, window.bring_to_front)
    code = app.exec()
    conn.close()
    log.info("dashboard exited with %d", code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
