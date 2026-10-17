"""Settings page: collection, collector control, startup, appearance, data, reset."""

from __future__ import annotations

import calendar
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from bytelog import __version__, paths
from bytelog.storage import repo, settings
from bytelog.storage.db import write_tx
from bytelog.storage.models import CollectorStatus
from bytelog.timeutil import now_utc_s
from bytelog.ui import theme
from bytelog.ui.context import AppContext
from bytelog.ui.dialogs import ResetDialog, confirm
from bytelog.ui.main_window import REFRESH_MS
from bytelog.ui.pages._common import (
    ago,
    db_file,
    fmt_local_ts,
    init_page,
    pad_spin,
    restyle_page,
    set_tone,
)
from bytelog.ui.sidebar import beat_stall_limit_s
from bytelog.ui.widgets import Card, Dot, danger_button, mono_label, muted_label

log = logging.getLogger(__name__)

TRANSITION_TIMEOUT_S = 15.0  # how long Start/Stop/Reset wait for the collector
FAST_POLL_MS = 500


def _setting_row(title: str, description: str, control: QWidget | QHBoxLayout) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(16)
    text = QVBoxLayout()
    text.setSpacing(2)
    text.addWidget(QLabel(title))
    if description:
        detail = muted_label(description)
        detail.setWordWrap(True)
        text.addWidget(detail)
    row.addLayout(text, 1)
    if isinstance(control, QWidget):
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    else:
        row.addLayout(control)
    return row


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {theme.tokens().border};")
    line.setProperty("divider", True)
    return line


class SettingsPage(QWidget):
    # (request number, "load" | "set", enabled, error message or None), from a worker thread
    _autostart_result = Signal(int, str, bool, object)
    _autostart_reload = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._autostart_seq = 0  # newest autostart request; older results are ignored
        self._autostart_busy = False
        self._autostart_reload_pending = False
        self._last_beat: int | None = None
        self._beat_seen_at = time.monotonic()
        self._pending: str | None = None  # "start" | "stop" while waiting for the collector
        self._pending_until = 0.0
        self._reset_token: str | None = None
        self._reset_forget = False
        self._reset_until = 0.0

        scaffold = init_page(self, "Settings", "Collection, startup, appearance and your data.")
        content = scaffold.content
        content.addWidget(self._build_collection())
        content.addWidget(self._build_collector())
        content.addWidget(self._build_appearance())
        content.addWidget(self._build_data())
        content.addWidget(self._build_danger())
        about = muted_label(f"ByteLog {__version__} · Your usage data never leaves this computer.")
        content.addWidget(about)
        content.addStretch(1)

        self._fast_timer = QTimer(self)
        self._fast_timer.setInterval(FAST_POLL_MS)
        self._fast_timer.timeout.connect(self._fast_poll)

        ctx.settings_changed.connect(lambda _key: self._sync_controls())
        self._autostart_result.connect(self._on_autostart_result)
        self._autostart_reload.connect(self._load_autostart)
        self._load_autostart()
        self.refresh()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not event.spontaneous():
            self._load_autostart()  # the task can change outside the app (or at startup)

    # ------------------------------------------------------------------ build
    def _build_collection(self) -> Card:
        card = Card("Collection")
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(settings.MIN_INTERVAL_S, settings.MAX_INTERVAL_S)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setKeyboardTracking(False)
        self.interval_spin.setMinimumWidth(90)
        pad_spin(self.interval_spin)
        self.interval_spin.valueChanged.connect(self._on_interval)
        card.body.addLayout(
            _setting_row(
                "Sampling interval",
                "How often the collector reads the adapter counters (5–60 seconds).",
                self.interval_spin,
            )
        )
        card.body.addWidget(_divider())
        self.week_combo = QComboBox()
        self.week_combo.addItems(list(calendar.day_name))
        self.week_combo.setMinimumWidth(140)
        self.week_combo.currentIndexChanged.connect(self._on_week_start)
        card.body.addLayout(
            _setting_row("Week starts on", "Used by “This week” on the Overview.", self.week_combo)
        )
        card.body.addWidget(_divider())
        self.pause_box = QCheckBox("Paused")
        self.pause_box.toggled.connect(self._on_pause)
        card.body.addLayout(
            _setting_row(
                "Pause collection",
                "While paused, traffic is not counted at all, not even later.",
                self.pause_box,
            )
        )
        return card

    def _build_collector(self) -> Card:
        card = Card("Collector")
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.status_dot = Dot(theme.tokens().muted)
        self.status_label = QLabel("Checking…")
        self.status_label.setStyleSheet("font-weight: 600;")
        status_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        self.stop_button = QPushButton("Stop collector")
        self.stop_button.clicked.connect(self.stop_collector)
        self.start_button = QPushButton("Start collector")
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(self.start_collector)
        status_row.addWidget(self.stop_button)
        status_row.addWidget(self.start_button)
        card.body.addLayout(status_row)
        self.status_detail = muted_label()
        self.status_detail.setWordWrap(True)
        card.body.addWidget(self.status_detail)
        explain = muted_label(
            "The collector records usage in the background, even when this window is closed. "
            "Once stopped it does not restart until you press Start or sign in to Windows again."
        )
        explain.setWordWrap(True)
        card.body.addWidget(explain)
        self.collector_error = QLabel()
        self.collector_error.setWordWrap(True)
        set_tone(self.collector_error, "danger")
        self.collector_error.hide()
        card.body.addWidget(self.collector_error)

        card.body.addWidget(_divider())
        self.autostart_box = QCheckBox("Enabled")
        self.autostart_box.toggled.connect(self._on_autostart)
        card.body.addLayout(
            _setting_row(
                "Start collector at login",
                "Starts recording when you sign in to Windows.",
                self.autostart_box,
            )
        )
        self.autostart_error = QLabel()
        self.autostart_error.setWordWrap(True)
        set_tone(self.autostart_error, "danger")
        self.autostart_error.hide()
        card.body.addWidget(self.autostart_error)
        return card

    def _build_appearance(self) -> Card:
        card = Card("Appearance")
        segmented = QFrame()
        segmented.setObjectName("Segmented")
        layout = QHBoxLayout(segmented)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.theme_group = QButtonGroup(self)
        self.theme_buttons: dict[str, QPushButton] = {}
        for key, text in (("light", "Light"), ("dark", "Dark")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, k=key: self.set_theme(k))
            self.theme_group.addButton(button)
            self.theme_buttons[key] = button
            layout.addWidget(button)
        card.body.addLayout(_setting_row("Theme", "", segmented))
        return card

    def _build_data(self) -> Card:
        card = Card("Data")
        card.body.addWidget(QLabel("Data location"))
        self.path_label = mono_label(str(self._db_path()))
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setWordWrap(True)
        card.body.addWidget(self.path_label)
        note = muted_label(
            "To keep data somewhere else, start ByteLog with --data-dir <folder> or set the "
            "BYTELOG_HOME environment variable. Keep it on a local drive (not a network or "
            "OneDrive folder)."
        )
        note.setWordWrap(True)
        card.body.addWidget(note)
        buttons = QHBoxLayout()
        open_folder = QPushButton("Open folder")
        open_folder.clicked.connect(self.open_data_folder)
        backup = QPushButton("Back up database…")
        backup.clicked.connect(self.choose_backup)
        buttons.addWidget(open_folder)
        buttons.addWidget(backup)
        buttons.addStretch(1)
        card.body.addLayout(buttons)
        self.data_status = QLabel()
        self.data_status.setWordWrap(True)
        self.data_status.hide()
        card.body.addWidget(self.data_status)
        return card

    def _build_danger(self) -> Card:
        card = Card("Danger zone")
        self.reset_button = danger_button("Reset all data…")
        self.reset_button.clicked.connect(self.ask_reset)
        card.body.addLayout(
            _setting_row(
                "Reset all data",
                "Deletes all recorded usage. You can also forget networks and plans.",
                self.reset_button,
            )
        )
        self.reset_status = QLabel()
        self.reset_status.setWordWrap(True)
        self.reset_status.hide()
        card.body.addWidget(self.reset_status)
        return card

    # ----------------------------------------------------------------- state
    def _db_path(self) -> Path:
        return db_file(self.ctx.conn) or paths.db_path()

    def refresh(self) -> None:
        self._sync_controls()
        self._update_status()

    def _sync_controls(self) -> None:
        try:
            values = settings.get_all(self.ctx.conn)
        except Exception:
            log.exception("reading settings failed")
            return
        conn = self.ctx.conn
        if not self.interval_spin.hasFocus():
            self._quiet(
                self.interval_spin,
                lambda: self.interval_spin.setValue(settings.sample_interval_s(conn)),
            )
        self._quiet(
            self.week_combo, lambda: self.week_combo.setCurrentIndex(settings.week_start(conn))
        )
        paused = values.get("collection_paused", "0") == "1"
        self._quiet(self.pause_box, lambda: self.pause_box.setChecked(paused))
        # The theme actually shown (a --theme flag can override the saved setting).
        name = "dark" if theme.tokens() is theme.DARK else "light"
        self.theme_buttons[name].setChecked(True)

    @staticmethod
    def _quiet(widget: QWidget, change: Callable[[], object]) -> None:
        widget.blockSignals(True)
        try:
            change()
        finally:
            widget.blockSignals(False)

    def _read_status(self) -> CollectorStatus | None:
        try:
            return repo.read_collector_status(self.ctx.conn)
        except Exception:
            log.exception("read_collector_status failed")
            return None

    def collector_state(self, status: CollectorStatus | None) -> str:
        """running | paused | stopped | stale, using the UI stale rule."""
        if status is None or status.state == "stopped":
            return "stopped"
        mono = time.monotonic()
        if status.beat != self._last_beat:
            self._last_beat = status.beat
            self._beat_seen_at = mono
        interval = settings.sample_interval_s(self.ctx.conn)
        heartbeat_age = now_utc_s() - status.heartbeat_utc if status.heartbeat_utc else None
        if heartbeat_age is None or heartbeat_age > max(60, 3 * interval):
            return "stale"
        # Polled by the 10 s page refresh (and the 500 ms fast poll while busy).
        if mono - self._beat_seen_at > beat_stall_limit_s(interval, REFRESH_MS / 1000):
            return "stale"
        return "paused" if status.state == "paused" else "running"

    def _update_status(self) -> None:
        status = self._read_status()
        state = self.collector_state(status)
        t = theme.tokens()
        now = time.monotonic()
        if self._pending:
            alive_now = state in ("running", "paused")
            if alive_now == (self._pending == "start"):
                self._pending = None
            elif now > self._pending_until:
                if self._pending == "start":
                    self._show_collector_error(
                        "The collector did not report in within 15 seconds. "
                        "See logs\\collector.log in the data folder."
                    )
                self._pending = None
        heartbeat = status.heartbeat_utc if status else None
        last = f"last sample {ago(now_utc_s() - heartbeat)}" if heartbeat else "no samples yet"
        if self._pending == "start":
            text, color = "Starting…", t.warn
        elif self._pending == "stop":
            text, color = "Stopping…", t.warn
        elif state == "running":
            text, color = f"Collector running · {last}", t.ok
        elif state == "paused":
            text, color = f"Collection paused · {last}", t.warn
        elif state == "stale":
            text, color = f"Collector not responding · {last}", t.danger
        else:
            text, color = "Collector stopped", t.danger
        self.status_label.setText(text)
        self.status_dot.set_color(color)
        alive = state in ("running", "paused")
        self.stop_button.setVisible(alive)
        self.start_button.setVisible(not alive)
        self.stop_button.setEnabled(self._pending is None)
        self.start_button.setEnabled(self._pending is None)
        details: list[str] = []
        if status and status.started_utc and alive:
            details.append(f"Started {fmt_local_ts(status.started_utc)}")
        if status and status.pid and alive:
            details.append(f"process {status.pid}")
        if status and status.app_version:
            details.append(f"version {status.app_version}")
        if status and status.last_error:
            details.append(f"last error: {status.last_error}")
        self.status_detail.setText(" · ".join(details))
        self.status_detail.setVisible(bool(details))
        self._update_fast_timer()

    def _update_fast_timer(self) -> None:
        busy = self._pending is not None or self._reset_token is not None
        if busy and not self._fast_timer.isActive():
            self._fast_timer.start()
        elif not busy and self._fast_timer.isActive():
            self._fast_timer.stop()

    def _fast_poll(self) -> None:
        if self._reset_token is not None:
            self._poll_reset()
        self._update_status()

    # ------------------------------------------------------------- settings
    def _write(self, key: str, value: object) -> None:
        try:
            settings.set_value(self.ctx.conn, key, value)
        except Exception:
            log.exception("could not save setting %s", key)
            return
        self.ctx.settings_changed.emit(key)

    def _on_interval(self, value: int) -> None:
        self._write("sample_interval_s", value)

    def _on_week_start(self, index: int) -> None:
        self._write("week_start", index)

    def _on_pause(self, paused: bool) -> None:
        self._write("collection_paused", paused)

    def set_theme(self, name: str) -> None:
        name = "dark" if name == "dark" else "light"
        try:
            settings.set_value(self.ctx.conn, "theme", name)
        except Exception:
            log.exception("could not save theme")
        self.theme_buttons[name].setChecked(True)
        self.ctx.theme_changed.emit(name)

    # ------------------------------------------------------------- autostart
    # schtasks/whoami can take seconds (up to their 20 s timeout) when Task Scheduler is
    # busy, so both the query and the change run off the UI thread.
    def reload_autostart_threadsafe(self) -> None:
        """Re-read the start-at-login state; callable from any thread."""
        self._autostart_reload.emit()

    @property
    def autostart_busy(self) -> bool:
        return self._autostart_busy

    def _load_autostart(self) -> None:
        if self._autostart_busy:
            self._autostart_reload_pending = True  # the running request may predate a change
            return
        platform = self.ctx.platform

        def query() -> bool:
            return platform.autostart_enabled()

        self._run_autostart("load", False, query)

    def _on_autostart(self, enabled: bool) -> None:
        platform = self.ctx.platform

        def change() -> bool:
            platform.set_autostart(enabled)
            return enabled

        self._run_autostart("set", enabled, change)

    def _run_autostart(self, op: str, enabled: bool, work: Callable[[], bool]) -> None:
        self._autostart_seq += 1
        seq = self._autostart_seq
        self._autostart_busy = True
        if op == "set":
            self._autostart_reload_pending = False  # the change itself sets the box
        self.autostart_box.setEnabled(False)

        def run() -> None:
            try:
                result, error = work(), None
            except Exception as exc:
                log.warning("autostart %s failed", op, exc_info=True)
                result, error = enabled, str(exc) or type(exc).__name__
            with contextlib.suppress(RuntimeError):  # the page was destroyed meanwhile
                self._autostart_result.emit(seq, op, result, error)

        threading.Thread(target=run, name=f"autostart-{op}", daemon=True).start()

    def _on_autostart_result(self, seq: int, op: str, enabled: bool, error: str | None) -> None:
        if seq != self._autostart_seq:
            return
        self._autostart_busy = False
        self.autostart_box.setEnabled(True)
        if self._autostart_reload_pending:
            self._autostart_reload_pending = False
            QTimer.singleShot(0, self._load_autostart)
        if op == "load":
            if error is not None:
                enabled = settings.get_bool(self.ctx.conn, "start_at_login")
            self._quiet(self.autostart_box, lambda: self.autostart_box.setChecked(enabled))
            return
        if error is not None:
            self._quiet(self.autostart_box, lambda: self.autostart_box.setChecked(not enabled))
            self.autostart_error.setText(f"Could not change start at login: {error}")
            self.autostart_error.show()
            return
        self.autostart_error.hide()
        self._write("start_at_login", enabled)

    # ------------------------------------------------------------- collector
    def _show_collector_error(self, message: str | None) -> None:
        self.collector_error.setText(message or "")
        self.collector_error.setVisible(bool(message))

    def stop_collector(self) -> None:
        if not confirm(
            self,
            "Stop the collector?",
            "ByteLog will not record any usage until you press Start collector, or until "
            "you next sign in to Windows if “Start collector at login” is on.",
            "Stop collector",
            danger=True,
        ):
            return
        self.request_stop()

    def request_stop(self) -> None:
        self._show_collector_error(None)
        try:
            settings.set_value(self.ctx.conn, "stop_requested", uuid.uuid4().hex)
        except Exception as exc:
            log.exception("could not write stop_requested")
            self._show_collector_error(f"Could not ask the collector to stop: {exc}")
            return
        try:
            self.ctx.platform.signal_stop()
        except Exception:
            log.warning("signal_stop failed; the collector stops on its next tick", exc_info=True)
        self.ctx.settings_changed.emit("stop_requested")
        self._pending, self._pending_until = "stop", time.monotonic() + TRANSITION_TIMEOUT_S
        self._update_status()

    def start_collector(self) -> None:
        self._show_collector_error(None)
        try:
            self.ctx.platform.launch_collector()
        except Exception as exc:
            log.exception("launch_collector failed")
            self._show_collector_error(f"Could not start the collector: {exc}")
            return
        self._pending, self._pending_until = "start", time.monotonic() + TRANSITION_TIMEOUT_S
        self._update_status()

    # ------------------------------------------------------------------ data
    def open_data_folder(self) -> None:
        try:
            self.ctx.platform.open_path(self._db_path().parent)
        except Exception as exc:
            self._show_data_status(f"Could not open the folder: {exc}", "danger")

    def choose_backup(self) -> None:
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        suggested = Path(folder or Path.home()) / f"ByteLog-backup-{date.today().isoformat()}.db"
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Back up database", str(suggested), "SQLite database (*.db)"
        )
        if chosen:
            self.backup_to(Path(chosen))

    def backup_to(self, dest: Path) -> bool:
        from bytelog.storage import maintenance

        if dest.resolve() == self._db_path().resolve():
            self._show_data_status("Choose a different file than the live database.", "danger")
            return False
        try:
            maintenance.backup_database(self.ctx.conn, dest)
        except Exception as exc:
            log.exception("backup failed")
            self._show_data_status(f"Backup failed: {exc}", "danger")
            return False
        self._show_data_status(f"Backed up to {dest}", "ok")
        return True

    def _show_data_status(self, text: str, tone: str) -> None:
        self.data_status.setText(text)
        set_tone(self.data_status, tone)
        self.data_status.show()

    # ----------------------------------------------------------------- reset
    def ask_reset(self) -> None:
        dialog = ResetDialog(self)
        if dialog.exec():
            self.reset_data(dialog.forget_networks)

    def reset_data(self, forget_networks: bool) -> None:
        """Reset through the running collector if there is one, else directly."""
        try:
            collector_running = self.ctx.platform.collector_lock_held()
        except Exception:
            log.warning("collector_lock_held failed", exc_info=True)
            collector_running = False
        if collector_running:
            token = uuid.uuid4().hex
            try:
                with write_tx(self.ctx.conn):
                    settings.set_value_in_tx(
                        self.ctx.conn, "reset_forget_networks", forget_networks
                    )
                    settings.set_value_in_tx(self.ctx.conn, "reset_requested", token)
            except Exception as exc:
                log.exception("could not request reset")
                self._show_reset_status(f"Reset failed: {exc}", "danger")
                return
            self._reset_token = token
            self._reset_forget = forget_networks
            self._reset_until = time.monotonic() + TRANSITION_TIMEOUT_S
            self.reset_button.setEnabled(False)
            self._show_reset_status("Resetting… waiting for the collector.", "warn")
            self._update_fast_timer()
            return
        from bytelog.storage import maintenance

        try:
            maintenance.reset_usage(self.ctx.conn, forget_networks)
        except Exception as exc:
            log.exception("reset_usage failed")
            self._show_reset_status(f"Reset failed: {exc}", "danger")
            return
        self._reset_succeeded(forget_networks)

    def _poll_reset(self) -> None:
        token = self._reset_token
        if token is None:
            return
        try:
            done = settings.get(self.ctx.conn, "reset_done") == token
        except Exception:
            log.exception("polling reset_done failed")
            done = False
        if done:
            self._reset_token = None
            self.reset_button.setEnabled(True)
            self._reset_succeeded(self._reset_forget)
        elif time.monotonic() > self._reset_until:
            self._reset_token = None
            self.reset_button.setEnabled(True)
            self._show_reset_status(
                "The collector did not confirm the reset within 15 seconds. It may still "
                "happen on its next sample; otherwise stop the collector and try again.",
                "danger",
            )
        self._update_fast_timer()

    def _reset_succeeded(self, forgot: bool) -> None:
        what = "usage, networks and plans were" if forgot else "usage data was"
        self._show_reset_status(f"Done: all recorded {what} deleted.", "ok")
        self.ctx.data_changed.emit()
        if forgot:
            self.ctx.plans_changed.emit()
            self.ctx.networks_changed.emit()

    def _show_reset_status(self, text: str, tone: str) -> None:
        self.reset_status.setText(text)
        set_tone(self.reset_status, tone)
        self.reset_status.show()

    def apply_theme(self) -> None:
        """Called by the main window after a theme switch."""
        restyle_page(self)
        border = theme.tokens().border
        for line in self.findChildren(QFrame):
            if line.property("divider"):
                line.setStyleSheet(f"background: {border};")
        self._update_status()
