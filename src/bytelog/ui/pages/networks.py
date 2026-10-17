"""Networks page: rename networks, mark them metered, include or exclude adapters."""

from __future__ import annotations

import dataclasses
import logging
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from bytelog import timeutil
from bytelog.platform.base import AdapterInfo
from bytelog.storage import repo, settings
from bytelog.storage.models import Adapter, Network
from bytelog.ui.context import AppContext
from bytelog.ui.pages._common import (
    KIND_LABELS,
    fit_table_height,
    fmt_local_ts,
    fmt_short_ts,
    hint_label,
    init_page,
    mono_font,
    restyle_page,
    set_tone,
    style_table,
    today_local,
)
from bytelog.ui.widgets import Card
from bytelog.units import format_bytes

log = logging.getLogger(__name__)

LOCATION_SETTINGS_URL = "ms-settings:privacy-location"
OVERRIDE_CHOICES: list[tuple[str, bool | None]] = [
    ("Default", None),
    ("Always include", True),
    ("Always exclude", False),
]
SNAPSHOT_TTL_S = 60.0


def _centered(widget: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignCenter)
    return holder


def _net_structure(net: Network) -> tuple[object, ...]:
    """What needs a table rebuild. Last seen and totals change every tick: updated in place."""
    return (net.id, net.kind, net.ssid, net.adapter_name, net.user_label, net.is_metered)


def _adapter_structure(adapter: Adapter) -> tuple[object, ...]:
    return (
        adapter.id,
        adapter.guid,
        adapter.name,
        adapter.description,
        adapter.kind,
        adapter.include_override,
    )


def _item(text: str, *, mono: bool = False, editable: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    flags = Qt.ItemFlag.ItemIsEnabled
    if editable:
        flags |= Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsSelectable
    item.setFlags(flags)
    if mono:
        item.setFont(mono_font())
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class NetworksPage(QWidget):
    NET_COLUMNS = ("Network", "Type", "First seen", "Last seen", "Metered", "Used, all time")
    ADAPTER_COLUMNS = ("Adapter", "Description", "Type", "Default rule", "Counting")

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._net_signature: object = None
        self._adapter_signature: object = None
        self._populating = False
        self._snapshot: dict[str, AdapterInfo] = {}
        self._snapshot_at = -SNAPSHOT_TTL_S
        self._labels: dict[int, tuple[str | None, str]] = {}  # id -> (user_label, default)
        self._net_rows: dict[int, int] = {}  # network id -> table row

        scaffold = init_page(
            self, "Networks", "Networks ByteLog has seen, and the adapters it counts."
        )
        content = scaffold.content

        self.location_card = Card()
        hint_row = QHBoxLayout()
        hint = QLabel(
            "Wi-Fi names need Location access: Settings > Privacy & security > Location. "
            "Until then Wi-Fi usage is recorded as “Wi-Fi (name unavailable)”."
        )
        hint.setWordWrap(True)
        set_tone(hint, "warn")
        open_button = QPushButton("Open Location settings")
        open_button.clicked.connect(self._open_location_settings)
        hint_row.addWidget(hint, 1)
        hint_row.addWidget(open_button)
        self.location_card.body.addLayout(hint_row)
        self.location_card.hide()
        content.addWidget(self.location_card)

        net_card = Card("Networks")
        net_card.body.addWidget(
            hint_label(
                "Double-click a name to rename it (clear it to go back to the detected name). "
                "Mark networks you pay for by the gigabyte as metered."
            )
        )
        self.net_table = QTableWidget(0, len(self.NET_COLUMNS))
        self.net_table.setHorizontalHeaderLabels(self.NET_COLUMNS)
        style_table(self.net_table)
        self.net_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.net_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.net_table.itemChanged.connect(self._on_label_edited)
        self.net_empty = hint_label(
            "No networks yet. The collector adds a network the first time it sees traffic on it."
        )
        net_card.body.addWidget(self.net_table)
        net_card.body.addWidget(self.net_empty)
        content.addWidget(net_card)

        adapter_card = Card("Adapters")
        adapter_card.body.addWidget(
            hint_label(
                "By default ByteLog counts physical Wi-Fi, Ethernet and cellular adapters and "
                "skips virtual ones (VPN, Hyper-V, WSL, Docker…). Changes apply from the next "
                "sample; a newly included adapter starts counting from that moment."
            )
        )
        self.adapter_table = QTableWidget(0, len(self.ADAPTER_COLUMNS))
        self.adapter_table.setHorizontalHeaderLabels(self.ADAPTER_COLUMNS)
        style_table(self.adapter_table, stretch_column=1, row_height=42)
        self.adapter_empty = hint_label("No adapters recorded yet. Is the collector running?")
        self.double_count_warning = QLabel(
            "An adapter that is excluded by default is now included. Virtual adapters such "
            "as VPNs usually carry traffic that also crosses a physical adapter, so totals "
            "may double count."
        )
        self.double_count_warning.setWordWrap(True)
        set_tone(self.double_count_warning, "warn")
        self.double_count_warning.hide()
        adapter_card.body.addWidget(self.adapter_table)
        adapter_card.body.addWidget(self.adapter_empty)
        adapter_card.body.addWidget(self.double_count_warning)
        content.addWidget(adapter_card)
        content.addStretch(1)

        ctx.networks_changed.connect(self.refresh)
        self.refresh()

    # ----------------------------------------------------------------- refresh
    def refresh(self) -> None:
        try:
            networks = repo.list_networks(self.ctx.conn)
            totals = self._all_time_totals()
            adapters = repo.list_adapters(self.ctx.conn)
        except Exception:
            log.exception("networks page refresh failed")
            return
        self.location_card.setVisible(any(n.kind == "wifi_unknown" for n in networks))
        # Rebuilding recreates the row widgets (metered checkboxes, "Counting" combos) under
        # the user's cursor, so it only happens when the structure changed; the per-tick
        # changes (totals, last seen) are written into the existing cells.
        if self.net_table.state() != QAbstractItemView.State.EditingState:
            signature = tuple(_net_structure(n) for n in networks)
            if signature != self._net_signature:
                self._net_signature = signature
                self._fill_networks(networks, totals)
            else:
                self._update_network_cells(networks, totals)
        defaults = self._default_rules(adapters)
        signature = (
            tuple(_adapter_structure(a) for a in adapters),
            tuple(sorted(defaults.items())),
        )
        if signature != self._adapter_signature:
            self._adapter_signature = signature
            self._fill_adapters(adapters, defaults)

    def _all_time_totals(self) -> dict[int, int]:
        rows = self.ctx.conn.execute(
            "SELECT network_id, SUM(rx_bytes) + SUM(tx_bytes) FROM daily GROUP BY network_id"
        ).fetchall()
        return {int(r[0]): int(r[1] or 0) for r in rows}

    def _fill_networks(self, networks: list[Network], totals: dict[int, int]) -> None:
        self._populating = True
        try:
            networks = sorted(networks, key=lambda n: (-totals.get(n.id, 0), n.label.lower()))
            table = self.net_table
            table.setRowCount(len(networks))
            self._labels.clear()
            self._net_rows.clear()
            tz = timeutil.local_zone()
            today = today_local(tz)
            for row, net in enumerate(networks):
                default_label = dataclasses.replace(net, user_label=None).label
                self._labels[net.id] = (net.user_label, default_label)
                self._net_rows[net.id] = row
                name = _item(net.label, editable=True)
                name.setData(Qt.ItemDataRole.UserRole, net.id)
                name.setToolTip(
                    f"Detected as: {default_label}" if net.user_label else "Double-click to rename"
                )
                table.setItem(row, 0, name)
                kind = "wifi" if net.kind == "wifi_unknown" else net.kind
                table.setItem(row, 1, _item(KIND_LABELS.get(kind, kind)))
                first = _item(fmt_short_ts(net.first_seen_utc, tz, today))
                first.setToolTip(fmt_local_ts(net.first_seen_utc, tz))
                table.setItem(row, 2, first)
                table.setItem(row, 3, _item(""))
                metered = QCheckBox()
                metered.setChecked(net.is_metered)
                metered.setToolTip("Metered: you pay for this network by the gigabyte")
                metered.toggled.connect(lambda checked, nid=net.id: self._set_metered(nid, checked))
                table.setCellWidget(row, 4, _centered(metered))
                table.setItem(row, 5, _item("", mono=True))
            self._update_network_cells(networks, totals)
            table.setVisible(bool(networks))
            self.net_empty.setVisible(not networks)
            fit_table_height(table)
        finally:
            self._populating = False

    def _update_network_cells(self, networks: list[Network], totals: dict[int, int]) -> None:
        tz = timeutil.local_zone()
        today = today_local(tz)
        was_populating, self._populating = self._populating, True
        try:
            for net in networks:
                row = self._net_rows.get(net.id)
                last = self.net_table.item(row, 3) if row is not None else None
                used = self.net_table.item(row, 5) if row is not None else None
                if last is None or used is None:
                    continue
                last.setText(fmt_short_ts(net.last_seen_utc, tz, today))
                last.setToolTip(fmt_local_ts(net.last_seen_utc, tz))
                used.setText(format_bytes(totals.get(net.id, 0)))
                used.setToolTip(f"{totals.get(net.id, 0):,} bytes")
        finally:
            self._populating = was_populating

    def _default_rules(self, adapters: list[Adapter]) -> dict[int, tuple[bool, str]]:
        """Default rule per adapter id: (counted, reason shown as a tooltip)."""
        try:
            from bytelog.collector.filter import matching_pattern, should_count
        except ImportError:
            return {}
        patterns = list(settings.get_json(self.ctx.conn, "exclude_patterns") or [])
        live = self._live_adapters()
        result: dict[int, tuple[bool, str]] = {}
        for adapter in adapters:
            info = live.get(adapter.guid) or AdapterInfo(
                name=adapter.name,
                guid=adapter.guid,
                description=adapter.description,
                kind=adapter.kind,  # type: ignore[arg-type]
                is_hardware=True,  # not stored; assume the OS flag passes when offline
                is_connected=False,
            )
            try:
                counted = should_count(info, patterns, None)
                pattern = matching_pattern(info, patterns)
            except Exception:
                log.exception("default rule failed for %s", adapter.name)
                continue
            if counted:
                reason = "Physical Wi-Fi, Ethernet or cellular adapter"
            elif pattern:
                reason = f"Name or description contains “{pattern}”"
            elif not info.is_hardware:
                reason = "Windows reports this as a virtual (software) adapter"
            else:
                reason = "Not a Wi-Fi, Ethernet or cellular adapter"
            result[adapter.id] = (counted, reason)
        return result

    def _live_adapters(self) -> dict[str, AdapterInfo]:
        now = time.monotonic()
        if now - self._snapshot_at >= SNAPSHOT_TTL_S:
            self._snapshot_at = now
            try:
                self._snapshot = dict(self.ctx.platform.snapshot().adapters)
            except Exception:
                log.warning("adapter snapshot failed", exc_info=True)
        return self._snapshot

    def _fill_adapters(
        self, adapters: list[Adapter], defaults: dict[int, tuple[bool, str]]
    ) -> None:
        table = self.adapter_table
        table.setRowCount(len(adapters))
        warn = False
        for row, adapter in enumerate(adapters):
            table.setItem(row, 0, _item(adapter.name))
            description = _item(adapter.description or "—")
            description.setToolTip(adapter.description)
            table.setItem(row, 1, description)
            table.setItem(row, 2, _item(KIND_LABELS.get(adapter.kind, adapter.kind)))
            default, reason = defaults.get(adapter.id, (None, ""))
            rule = "—" if default is None else ("Counted" if default else "Excluded")
            rule_label = QLabel(rule)
            rule_label.setContentsMargins(6, 0, 6, 0)
            rule_label.setToolTip(reason)
            if default is not None:
                set_tone(rule_label, "ok" if default else "muted")
            table.setCellWidget(row, 3, rule_label)
            combo = QComboBox()
            for text, value in OVERRIDE_CHOICES:
                combo.addItem(text, value)
            combo.setCurrentIndex(
                next(
                    i for i, (_, v) in enumerate(OVERRIDE_CHOICES) if v == adapter.include_override
                )
            )
            combo.currentIndexChanged.connect(
                lambda index, aid=adapter.id: self._set_override(aid, OVERRIDE_CHOICES[index][1])
            )
            table.setCellWidget(row, 4, combo)
            if adapter.include_override is True and default is False:
                warn = True
        table.setVisible(bool(adapters))
        self.adapter_empty.setVisible(not adapters)
        self.double_count_warning.setVisible(warn)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(False)
        fit_table_height(table)

    # ------------------------------------------------------------------ writes
    def _on_label_edited(self, item: QTableWidgetItem) -> None:
        if self._populating or item.column() != 0:
            return
        network_id = int(item.data(Qt.ItemDataRole.UserRole))
        current, default = self._labels.get(network_id, (None, ""))
        text = item.text().strip()
        new_label = None if not text or text == default else text
        if new_label == current:
            if not text:
                self._populating = True
                item.setText(default)
                self._populating = False
            return
        try:
            repo.update_network(self.ctx.conn, network_id, user_label=new_label)
        except Exception:
            log.exception("rename failed")
            self._net_signature = None
            self.refresh()
            return
        self._net_signature = None
        self._announce()

    def _set_metered(self, network_id: int, metered: bool) -> None:
        try:
            repo.update_network(self.ctx.conn, network_id, is_metered=metered)
        except Exception:
            log.exception("metered update failed")
        self._announce()

    def _set_override(self, adapter_id: int, include: bool | None) -> None:
        try:
            repo.set_adapter_override(self.ctx.conn, adapter_id, include)
        except Exception:
            log.exception("adapter override failed")
        self._announce()

    def _announce(self) -> None:
        # Deferred: listeners (including this page) may rebuild the row that sent it.
        QTimer.singleShot(0, self.ctx.networks_changed.emit)

    def _open_location_settings(self) -> None:
        QDesktopServices.openUrl(QUrl(LOCATION_SETTINGS_URL))

    def apply_theme(self) -> None:
        """Called by the main window after a theme switch."""
        restyle_page(self)
