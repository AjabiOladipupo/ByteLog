"""Reports page: pick a date range and network, preview the numbers, export CSV or PDF."""

from __future__ import annotations

import calendar
import logging
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRunnable,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QWidget,
)

from bytelog import paths
from bytelog.storage import periods, repo
from bytelog.storage.db import connect
from bytelog.storage.models import Bucket, DailyRow
from bytelog.ui import theme
from bytelog.ui.context import AppContext
from bytelog.ui.dialogs import show_message
from bytelog.ui.pages._common import (
    MONTHS,
    db_file,
    fit_table_height,
    fmt_day,
    init_page,
    mono_font,
    restyle_page,
    set_tone,
    style_table,
    today_local,
)
from bytelog.ui.widgets import Card, ProgressBar, mono_label, muted_label
from bytelog.units import format_bytes, percent, split_value_unit

log = logging.getLogger(__name__)

PRESETS = ("This month", "Last month", "Last 30 days", "This year")
MONTHLY_CHART_AFTER_DAYS = 120  # longer ranges chart one bar per month
TABLE_MAX_VISIBLE_ROWS = 14


def preset_range(name: str, today: date) -> tuple[date, date]:
    """Inclusive (start, end) for a quick preset."""
    if name == "This month":
        return today.replace(day=1), today
    if name == "Last month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if name == "Last 30 days":
        return today - timedelta(days=29), today
    if name == "This year":
        return date(today.year, 1, 1), today
    raise ValueError(name)


def default_export_name(start: date, end: date, ext: str) -> str:
    return f"ByteLog_{start.isoformat()}_to_{end.isoformat()}.{ext}"


def monthly_buckets(days: list[Bucket]) -> list[Bucket]:
    """Fold daily buckets into one bucket per calendar month."""
    months: OrderedDict[tuple[int, int], list[int]] = OrderedDict()
    for bucket in days:
        start = bucket.start
        key = (start.year, start.month)
        totals = months.setdefault(key, [0, 0])
        totals[0] += bucket.rx
        totals[1] += bucket.tx
    return [
        Bucket(
            label=f"{MONTHS[m - 1]} {str(y)[2:]}",
            tooltip_title=f"{calendar.month_name[m]} {y}",
            start=date(y, m, 1),
            rx=rx,
            tx=tx,
        )
        for (y, m), (rx, tx) in months.items()
    ]


def _qdate(day: date) -> QDate:
    return QDate(day.year, day.month, day.day)


def _pydate(qdate: QDate) -> date:
    return date(qdate.year(), qdate.month(), qdate.day())


class DailyRowsModel(QAbstractTableModel):
    HEADERS = ("Date", "Network", "Download", "Upload", "Total")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[DailyRow] = []
        self._mono = mono_font()

    def set_rows(self, rows: list[DailyRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:  # noqa: N802
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def columnCount(  # noqa: N802 - Qt API
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.HEADERS)

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.HEADERS[section]
            if role == Qt.ItemDataRole.TextAlignmentRole and section >= 2:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return fmt_day(row.day)
            if col == 1:
                return row.network_label
            return format_bytes((row.rx, row.tx, row.total)[col - 2])
        if role == Qt.ItemDataRole.ToolTipRole and col >= 2:
            return f"{(row.rx, row.tx, row.total)[col - 2]:,} bytes"
        if col >= 2:
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            if role == Qt.ItemDataRole.FontRole:
                return self._mono
        return None


class _ExportSignals(QObject):
    finished = Signal(str)  # output path
    failed = Signal(str)  # message


class ExportJob(QRunnable):
    """Writes a CSV or PDF on a pool thread with its own SQLite connection."""

    def __init__(
        self,
        kind: str,
        db: Path,
        dest: Path,
        start: date,
        end: date,
        network_ids: list[int] | None,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.db = db
        self.dest = dest
        self.start = start
        self.end = end
        self.network_ids = network_ids
        self.signals = _ExportSignals()

    def run(self) -> None:
        conn = None
        try:
            conn = connect(self.db)
            if self.kind == "csv":
                from bytelog.reports.csv_export import export_csv

                export_csv(conn, self.dest, self.start, self.end, self.network_ids)
            else:
                from bytelog.reports.pdf_report import export_pdf

                export_pdf(conn, self.dest, self.start, self.end, self.network_ids)
        except Exception as exc:
            log.exception("%s export failed", self.kind)
            self.signals.failed.emit(str(exc) or type(exc).__name__)
        else:
            self.signals.finished.emit(str(self.dest))
        finally:
            if conn is not None:
                conn.close()


class _SummaryTile(Card):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.body.addWidget(muted_label(title))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.value = QLabel("0")
        self.value.setObjectName("BigNumber")
        self.unit = QLabel("MB")
        self.unit.setObjectName("BigUnit")
        row.addWidget(self.value, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addStretch(1)
        self.body.addLayout(row)
        self.detail = muted_label()
        self.body.addWidget(self.detail)

    def set(self, n: int, detail: str) -> None:
        value, unit = split_value_unit(n)
        self.value.setText(value)
        self.unit.setText(unit)
        self.value.setToolTip(f"{n:,} bytes")
        self.detail.setText(detail)


class ReportsPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._network_signature: object = None
        self._breakdown_signature: object = None
        self._jobs: set[ExportJob] = set()
        self._last_export: Path | None = None
        self._export_dir: Path | None = None
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(150)
        self._update_timer.timeout.connect(self.update_preview)

        scaffold = init_page(self, "Reports", "")
        self.subtitle = scaffold.subtitle
        self.csv_button = QPushButton("Export CSV")
        self.pdf_button = QPushButton("Export PDF")
        self.pdf_button.setProperty("primary", True)
        self.csv_button.clicked.connect(lambda: self.choose_and_export("csv"))
        self.pdf_button.clicked.connect(lambda: self.choose_and_export("pdf"))
        scaffold.actions.addWidget(self.csv_button, 0, Qt.AlignmentFlag.AlignTop)
        scaffold.actions.addWidget(self.pdf_button, 0, Qt.AlignmentFlag.AlignTop)
        content = scaffold.content

        content.addWidget(self._build_export_status())
        content.addWidget(self._build_filters())

        tiles = QHBoxLayout()
        tiles.setSpacing(16)
        self.total_tile = _SummaryTile("Total used")
        self.down_tile = _SummaryTile("Downloaded")
        self.up_tile = _SummaryTile("Uploaded")
        for tile in (self.total_tile, self.down_tile, self.up_tile):
            tiles.addWidget(tile, 1)
        content.addLayout(tiles)

        self.chart = self._make_chart()
        if self.chart is not None:
            chart_card = Card("Usage over the range")
            self.chart.setFixedHeight(260)
            chart_card.body.addWidget(self.chart)
            content.addWidget(chart_card)

        networks_card = Card("By network")
        self.network_rows = QGridLayout()
        self.network_rows.setHorizontalSpacing(14)
        self.network_rows.setVerticalSpacing(8)
        self.network_rows.setColumnStretch(2, 1)
        networks_card.body.addLayout(self.network_rows)
        content.addWidget(networks_card)

        table_card = Card("Daily breakdown")
        self.table_caption = muted_label()
        table_card.body.addWidget(self.table_caption)
        self.model = DailyRowsModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        style_table(self.table, stretch_column=1)
        header = self.table.horizontalHeader()
        for col in (2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(col, 110)
        table_card.body.addWidget(self.table)
        content.addWidget(table_card)
        content.addStretch(1)

        start, end = preset_range("This month", today_local())
        self._set_range(start, end, preset="This month")
        self._sync_networks()
        ctx.data_changed.connect(self._on_data_changed)
        ctx.networks_changed.connect(self._on_networks_changed)
        self.update_preview()

    # ------------------------------------------------------------------ build
    def _build_filters(self) -> Card:
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        self.start_edit = QDateEdit()
        self.end_edit = QDateEdit()
        for edit in (self.start_edit, self.end_edit):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("d MMM yyyy")
            edit.setMinimumWidth(130)
            edit.dateChanged.connect(self._on_date_edited)
        self.network_combo = QComboBox()
        self.network_combo.setMinimumWidth(200)
        self.network_combo.currentIndexChanged.connect(lambda _i: self._schedule_update())
        row.addWidget(QLabel("From"))
        row.addWidget(self.start_edit)
        row.addWidget(QLabel("to"))
        row.addWidget(self.end_edit)
        row.addSpacing(12)
        row.addWidget(QLabel("Network"))
        row.addWidget(self.network_combo)
        row.addStretch(1)
        card.body.addLayout(row)

        presets = QFrame()
        presets.setObjectName("Segmented")
        presets_layout = QHBoxLayout(presets)
        presets_layout.setContentsMargins(3, 3, 3, 3)
        presets_layout.setSpacing(2)
        self.preset_group = QButtonGroup(self)
        self.preset_buttons: dict[str, QPushButton] = {}
        for name in PRESETS:
            button = QPushButton(name)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c=False, n=name: self.apply_preset(n))
            self.preset_group.addButton(button)
            self.preset_buttons[name] = button
            presets_layout.addWidget(button)
        preset_row = QHBoxLayout()
        preset_row.addWidget(presets)
        preset_row.addStretch(1)
        card.body.addLayout(preset_row)
        return card

    def _build_export_status(self) -> QWidget:
        self.export_card = Card()
        self.export_card.body.setContentsMargins(16, 10, 12, 10)
        row = QHBoxLayout()
        self.export_label = QLabel()
        self.export_label.setWordWrap(True)
        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(self._open_last_export)
        self.open_folder_button = QPushButton("Show folder")
        self.open_folder_button.clicked.connect(self._open_last_export_folder)
        row.addWidget(self.export_label, 1)
        row.addWidget(self.open_button)
        row.addWidget(self.open_folder_button)
        self.export_card.body.addLayout(row)
        self.export_card.hide()
        return self.export_card

    def _make_chart(self) -> QWidget | None:
        try:
            from bytelog.ui.charts.stacked_bar import StackedBarChart
        except ImportError:
            return None
        try:
            return StackedBarChart()
        except Exception:
            log.exception("could not create the report chart")
            return None

    # ------------------------------------------------------------------ state
    def date_range(self) -> tuple[date, date]:
        return _pydate(self.start_edit.date()), _pydate(self.end_edit.date())

    def network_ids(self) -> list[int] | None:
        value = self.network_combo.currentData()
        return None if value is None else [int(value)]

    def apply_preset(self, name: str) -> None:
        start, end = preset_range(name, today_local())
        self._set_range(start, end, preset=name)
        self._schedule_update()

    def _set_range(self, start: date, end: date, preset: str | None = None) -> None:
        for edit, day in ((self.start_edit, start), (self.end_edit, end)):
            edit.blockSignals(True)
            edit.setDate(_qdate(day))
            edit.blockSignals(False)
        self._check_preset(preset)

    def _check_preset(self, preset: str | None) -> None:
        self.preset_group.setExclusive(False)
        for name, button in self.preset_buttons.items():
            button.setChecked(name == preset)
        self.preset_group.setExclusive(True)

    def _on_date_edited(self) -> None:
        start, end = self.date_range()
        if start > end:  # keep the range valid: move the other end
            if self.sender() is self.start_edit:
                self._set_range(start, start)
            else:
                self._set_range(end, end)
        self._check_preset(None)
        self._schedule_update()

    def _schedule_update(self) -> None:
        self._update_timer.start()

    def _sync_networks(self) -> None:
        try:
            networks = repo.list_networks(self.ctx.conn)
        except Exception:
            log.exception("list_networks failed")
            return
        signature = tuple((n.id, n.label) for n in networks)
        if signature == self._network_signature:
            return
        self._network_signature = signature
        current = self.network_combo.currentData()
        self.network_combo.blockSignals(True)
        self.network_combo.clear()
        self.network_combo.addItem("All networks", None)
        for network in sorted(networks, key=lambda n: n.label.lower()):
            self.network_combo.addItem(network.label, network.id)
        index = self.network_combo.findData(current) if current is not None else 0
        self.network_combo.setCurrentIndex(max(0, index))
        self.network_combo.blockSignals(False)

    # ---------------------------------------------------------------- preview
    def refresh(self) -> None:
        self._sync_networks()
        if self.date_range()[1] >= today_local():
            self.update_preview()

    def _on_data_changed(self) -> None:
        if self.isVisible():
            self.refresh()

    def _on_networks_changed(self) -> None:
        self._sync_networks()
        self._schedule_update()

    def update_preview(self) -> None:
        start, end = self.date_range()
        ids = self.network_ids()
        try:
            rows = periods.daily_rows(self.ctx.conn, start, end, ids)
        except Exception:
            log.exception("daily_rows failed")
            rows = []
        days = (end - start).days + 1
        self.subtitle.setText(self._range_label(start, end))
        rx = sum(r.rx for r in rows)
        tx = sum(r.tx for r in rows)
        total = rx + tx
        self.total_tile.set(total, f"over {days} day{'s' if days != 1 else ''}")
        self.down_tile.set(rx, f"{percent(rx, total):.0f}% of total")
        self.up_tile.set(tx, f"{percent(tx, total):.0f}% of total")
        self.model.set_rows(rows)
        self.table_caption.setText(
            f"{len(rows)} row{'s' if len(rows) != 1 else ''} · one per network per day with usage"
        )
        self.table.setVisible(bool(rows))
        fit_table_height(self.table, TABLE_MAX_VISIBLE_ROWS)
        self._fill_network_breakdown(rows, total)
        if self.chart is not None:
            try:
                buckets = periods.daily_totals(self.ctx.conn, start, end, ids)
                if days > MONTHLY_CHART_AFTER_DAYS:
                    buckets = monthly_buckets(buckets)
                self.chart.set_buckets(buckets)  # type: ignore[attr-defined]
            except Exception:
                log.exception("report chart update failed")

    def _range_label(self, start: date, end: date) -> str:
        try:
            return periods.date_range_label(start, end)
        except Exception:
            return f"{fmt_day(start)} – {fmt_day(end)}"

    def _fill_network_breakdown(self, rows: list[DailyRow], total: int) -> None:
        per: dict[int, tuple[str, int, int]] = {}
        for r in rows:
            label, rx, tx = per.get(r.network_id, (r.network_label, 0, 0))
            per[r.network_id] = (label, rx + r.rx, tx + r.tx)
        ordered = sorted(per.values(), key=lambda e: -(e[1] + e[2]))
        signature = (tuple(ordered), theme.tokens())
        if signature == self._breakdown_signature:
            return
        self._breakdown_signature = signature
        grid = self.network_rows
        while grid.count():
            item = grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        if not ordered:
            grid.addWidget(muted_label("No usage recorded in this range."), 0, 0, 1, 4)
            return
        t = theme.tokens()
        for i, (label, rx, tx) in enumerate(ordered):
            used = rx + tx
            name = QLabel(label)
            amount = mono_label(format_bytes(used))
            amount.setToolTip(f"Download {format_bytes(rx)} · Upload {format_bytes(tx)}")
            bar = ProgressBar(color=t.download, height=6)
            bar.set_value(used / total if total else 0.0)
            share = mono_label(f"{percent(used, total):.0f}%")
            share.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            share.setMinimumWidth(44)
            grid.addWidget(name, i, 0)
            grid.addWidget(amount, i, 1, Qt.AlignmentFlag.AlignRight)
            grid.addWidget(bar, i, 2, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(share, i, 3)

    # ----------------------------------------------------------------- export
    def choose_and_export(self, kind: str) -> None:
        from PySide6.QtWidgets import QFileDialog

        start, end = self.date_range()
        folder = self._export_dir or Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            or str(Path.home())
        )
        suggested = str(folder / default_export_name(start, end, kind))
        filters = "CSV files (*.csv)" if kind == "csv" else "PDF files (*.pdf)"
        title = "Export CSV" if kind == "csv" else "Export PDF report"
        chosen, _ = QFileDialog.getSaveFileName(self, title, suggested, filters)
        if not chosen:
            return
        dest = Path(chosen)
        if dest.suffix.lower() != f".{kind}":
            dest = dest.with_suffix(f".{kind}")
        self._export_dir = dest.parent
        self.export_to(kind, dest)

    def export_to(self, kind: str, dest: Path) -> ExportJob:
        """Start an export in the background; the status card reports the result."""
        start, end = self.date_range()
        db = db_file(self.ctx.conn) or paths.db_path()
        job = ExportJob(kind, db, dest, start, end, self.network_ids())
        job.signals.finished.connect(lambda path, j=job: self._export_finished(j, path))
        job.signals.failed.connect(lambda message, j=job: self._export_failed(j, message))
        self._jobs.add(job)
        self._set_exporting(True)
        self._show_status(f"Exporting {kind.upper()}…", None, can_open=False)
        QThreadPool.globalInstance().start(job)
        return job

    def _set_exporting(self, busy: bool) -> None:
        self.csv_button.setEnabled(not busy)
        self.pdf_button.setEnabled(not busy)

    def _export_finished(self, job: ExportJob, path: str) -> None:
        self._jobs.discard(job)
        self._set_exporting(bool(self._jobs))
        self._last_export = Path(path)
        self._show_status(f"Saved {Path(path).name}", "ok", can_open=True)

    def _export_failed(self, job: ExportJob, message: str) -> None:
        self._jobs.discard(job)
        self._set_exporting(bool(self._jobs))
        self._show_status(f"Export failed: {message}", "danger", can_open=False)

    def _show_status(self, text: str, tone: str | None, *, can_open: bool) -> None:
        self.export_label.setText(text)
        set_tone(self.export_label, tone)
        self.open_button.setVisible(can_open)
        self.open_folder_button.setVisible(can_open)
        self.export_card.show()

    def _open_last_export(self) -> None:
        if self._last_export is not None:
            self._open(self._last_export)

    def _open_last_export_folder(self) -> None:
        if self._last_export is not None:
            self._open(self._last_export.parent)

    def _open(self, path: Path) -> None:
        try:
            self.ctx.platform.open_path(path)
        except Exception as exc:
            show_message(self, "Could not open", str(exc))

    def apply_theme(self) -> None:
        """Called by the main window after a theme switch."""
        restyle_page(self)
        apply_chart_theme = getattr(self.chart, "apply_theme", None)
        if apply_chart_theme is not None:
            apply_chart_theme()
        self.update_preview()
