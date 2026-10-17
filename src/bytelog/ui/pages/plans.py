"""Plans and alerts page: data plans with live cycle status, and the recent alert log."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from bytelog.storage import plans as plan_store
from bytelog.storage import repo, settings
from bytelog.storage.models import Network, Plan, PlanStatus
from bytelog.ui import theme
from bytelog.ui.context import AppContext
from bytelog.ui.dialogs import PlanEditorDialog, confirm, show_message
from bytelog.ui.pages._common import (
    fit_table_height,
    fmt_day,
    fmt_local_ts,
    hint_label,
    init_page,
    mono_font,
    restyle_page,
    set_tone,
    style_table,
    today_local,
)
from bytelog.ui.widgets import Card, ProgressBar, mono_label, muted_label, primary_button
from bytelog.units import format_bytes, split_value_unit

log = logging.getLogger(__name__)

RECENT_ALERTS = 20


def cycle_description(plan: Plan) -> str:
    if plan.cycle_type == "rolling_days":
        start = f" from {fmt_day(plan.anchor_date)}" if plan.anchor_date else ""
        return f"Every {plan.cycle_value} days{start}"
    return f"Monthly, resets on day {plan.cycle_value}"


def networks_description(plan: Plan, labels: dict[int, str]) -> str:
    if not plan.network_ids:
        return "All networks"
    names = [labels.get(nid, f"network {nid}") for nid in plan.network_ids]
    return ", ".join(names)


def thresholds_description(plan: Plan) -> str:
    if not plan.thresholds:
        return "No alerts"
    return "Alerts at " + ", ".join(f"{t}%" for t in plan.thresholds)


class PlanCard(Card):
    """One plan: definition line, usage against the cap, cycle and allowance."""

    def __init__(self, page: PlansPage, plan: Plan, labels: dict[int, str]) -> None:
        super().__init__()
        self.page = page
        self.plan = plan

        top = QHBoxLayout()
        top.setSpacing(10)
        name = QLabel(plan.name)
        name.setObjectName("CardTitle")
        name.setStyleSheet("font-size: 15px;")
        top.addWidget(name)
        self.badge = QLabel()
        top.addWidget(self.badge)
        top.addStretch(1)
        self.active_box = QCheckBox("Active")
        self.active_box.setChecked(plan.active)
        self.active_box.toggled.connect(lambda on: page.set_active(plan, on))
        top.addWidget(self.active_box)
        edit = QPushButton("Edit")
        edit.clicked.connect(lambda: page.edit_plan(plan))
        delete = QPushButton("Delete")
        delete.clicked.connect(lambda: page.delete_plan(plan))
        top.addWidget(edit)
        top.addWidget(delete)
        self.body.addLayout(top)

        definition = muted_label(
            " · ".join(
                [
                    f"{format_bytes(plan.cap_bytes)} cap",
                    cycle_description(plan),
                    networks_description(plan, labels),
                    thresholds_description(plan),
                ]
            )
        )
        definition.setWordWrap(True)
        self.body.addWidget(definition)
        self.body.addSpacing(4)

        usage = QHBoxLayout()
        usage.setSpacing(6)
        self.used = mono_label()
        self.used.setStyleSheet("font-size: 20px; font-weight: 500;")
        self.of_cap = muted_label()
        self.percent = mono_label()
        self.percent.setStyleSheet("font-size: 15px;")
        usage.addWidget(self.used, 0, Qt.AlignmentFlag.AlignBaseline)
        usage.addWidget(self.of_cap, 0, Qt.AlignmentFlag.AlignBaseline)
        usage.addStretch(1)
        usage.addWidget(self.percent, 0, Qt.AlignmentFlag.AlignBaseline)
        self.body.addLayout(usage)
        self.bar = ProgressBar(height=8)
        self.body.addWidget(self.bar)
        self.cycle_line = muted_label()
        self.cycle_line.setWordWrap(True)
        self.body.addWidget(self.cycle_line)
        self.over_line = QLabel()
        set_tone(self.over_line, "danger")
        self.over_line.hide()
        self.body.addWidget(self.over_line)
        if not plan.active:
            self.badge.setText("Inactive")
            set_tone(self.badge, "muted")

    def set_status(self, status: PlanStatus | None) -> None:
        t = theme.tokens()
        if status is None:
            self.used.setText("—")
            self.of_cap.setText(f"of {format_bytes(self.plan.cap_bytes)}")
            self.percent.setText("")
            self.bar.set_value(0)
            self.cycle_line.setText("Usage unavailable.")
            return
        value, unit = split_value_unit(status.used)
        self.used.setText(
            f'{value}<span style="font-size: 13px; color: {t.muted};">&nbsp;{unit}</span>'
        )
        self.of_cap.setText(f"of {format_bytes(self.plan.cap_bytes)}")
        self.percent.setText(f"{status.percent:.0f}%")
        self.bar.set_value(status.percent / 100.0)
        self.bar.set_color(t.danger if status.over_by > 0 else t.plan)
        if status.cycle is None:
            start = self.plan.anchor_date
            self.cycle_line.setText(
                f"Starts on {fmt_day(start)}." if start else "The first cycle has not started."
            )
        else:
            cycle = status.cycle
            days = "1 day left" if status.days_left == 1 else f"{status.days_left} days left"
            parts = [f"{fmt_day(cycle.start)} – {fmt_day(cycle.last_day)}", days]
            if status.over_by <= 0:
                parts.append(f"{format_bytes(status.daily_allowance)} a day to stay under")
            self.cycle_line.setText(" · ".join(parts))
        self.over_line.setVisible(status.over_by > 0)
        if status.over_by > 0:
            self.over_line.setText(f"Over the cap by {format_bytes(status.over_by)}.")


class PlansPage(QWidget):
    ALERT_COLUMNS = ("Plan", "Alert", "When", "Used at the time")

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._plans_signature: object = None
        self._alerts_signature: object = None
        self._cards: list[PlanCard] = []
        self._network_labels: dict[int, str] = {}

        scaffold = init_page(
            self, "Plans and alerts", "Track a data cap per billing cycle and get alerts."
        )
        new_button = primary_button("New plan")
        new_button.clicked.connect(self.new_plan)
        scaffold.actions.addWidget(new_button, 0, Qt.AlignmentFlag.AlignTop)
        content = scaffold.content

        self.plans_box = QVBoxLayout()
        self.plans_box.setSpacing(16)
        content.addLayout(self.plans_box)

        self.empty_card = Card("No plans yet")
        self.empty_card.body.addWidget(
            hint_label(
                "Add your data plan (for example a 30 GB MiFi bundle that renews on the 5th) "
                "to see how much is left in the cycle and get a notification at 80% and 100%."
            )
        )
        empty_button = primary_button("Create a plan")
        empty_button.clicked.connect(self.new_plan)
        row = QHBoxLayout()
        row.addWidget(empty_button)
        row.addStretch(1)
        self.empty_card.body.addLayout(row)
        content.addWidget(self.empty_card)

        alerts_card = Card("Recent alerts")
        self.alerts_table = QTableWidget(0, len(self.ALERT_COLUMNS))
        self.alerts_table.setHorizontalHeaderLabels(self.ALERT_COLUMNS)
        style_table(self.alerts_table)
        self.alerts_empty = muted_label("No alerts yet.")
        alerts_card.body.addWidget(self.alerts_table)
        alerts_card.body.addWidget(self.alerts_empty)
        content.addWidget(alerts_card)
        content.addStretch(1)

        ctx.plans_changed.connect(self._force_refresh)
        ctx.networks_changed.connect(self._force_refresh)
        self.refresh()

    # ----------------------------------------------------------------- refresh
    def refresh(self) -> None:
        try:
            plans = plan_store.list_plans(self.ctx.conn)
            networks = repo.list_networks(self.ctx.conn)
        except Exception:
            log.exception("plans page refresh failed")
            return
        self._network_labels = {n.id: n.label for n in networks}
        signature = (tuple(_plan_key(p) for p in plans), tuple(self._network_labels.items()))
        if signature != self._plans_signature:
            self._plans_signature = signature
            self._rebuild_cards(plans)
        today = today_local()
        for card in self._cards:
            try:
                status = plan_store.plan_status(self.ctx.conn, card.plan, today)
            except Exception:
                log.exception("plan_status failed for plan %s", card.plan.id)
                status = None
            card.set_status(status)
        self._refresh_alerts()

    def _force_refresh(self) -> None:
        self._plans_signature = None
        self._alerts_signature = None
        self.refresh()

    def _rebuild_cards(self, plans: list[Plan]) -> None:
        for card in self._cards:
            self.plans_box.removeWidget(card)
            card.deleteLater()
        self._cards = [PlanCard(self, plan, self._network_labels) for plan in plans]
        for card in self._cards:
            self.plans_box.addWidget(card)
        self.empty_card.setVisible(not plans)

    def _refresh_alerts(self) -> None:
        rows = self.ctx.conn.execute(
            "SELECT p.name, a.threshold, a.sent_at_utc, a.used_bytes, p.cap_bytes "
            "FROM alerts_sent a JOIN plans p ON p.id = a.plan_id "
            "ORDER BY a.sent_at_utc DESC LIMIT ?",
            (RECENT_ALERTS,),
        ).fetchall()
        signature = tuple(tuple(r) for r in rows)
        if signature == self._alerts_signature:
            return
        self._alerts_signature = signature
        table = self.alerts_table
        table.setRowCount(len(rows))
        for i, (name, threshold, sent_at, used, cap) in enumerate(rows):
            table.setItem(i, 0, QTableWidgetItem(str(name)))
            table.setItem(i, 1, QTableWidgetItem(f"{threshold}% of {format_bytes(cap)}"))
            table.setItem(i, 2, QTableWidgetItem(fmt_local_ts(int(sent_at))))
            used_item = QTableWidgetItem(format_bytes(int(used)))
            used_item.setFont(mono_font())
            table.setItem(i, 3, used_item)
        table.setVisible(bool(rows))
        self.alerts_empty.setVisible(not rows)
        fit_table_height(table)

    # ----------------------------------------------------------------- actions
    def _networks(self) -> list[Network]:
        try:
            return repo.list_networks(self.ctx.conn)
        except Exception:
            log.exception("list_networks failed")
            return []

    def _open_editor(self, plan: Plan | None) -> None:
        dialog = PlanEditorDialog(
            self.ctx.conn,
            self._networks(),
            plan,
            default_thresholds=settings.get(self.ctx.conn, "default_alert_thresholds"),
            today=today_local(),
            parent=self,
        )
        if dialog.exec():
            self._changed()

    def new_plan(self) -> None:
        self._open_editor(None)

    def edit_plan(self, plan: Plan) -> None:
        current = plan_store.get_plan(self.ctx.conn, plan.id)
        if current is None:
            self._changed()
            return
        self._open_editor(current)

    def delete_plan(self, plan: Plan) -> None:
        if not confirm(
            self,
            "Delete plan",
            f"Delete “{plan.name}”? Its alert history is deleted too. "
            "Recorded usage is not affected.",
            "Delete plan",
            danger=True,
        ):
            return
        try:
            plan_store.delete_plan(self.ctx.conn, plan.id)
        except Exception as exc:
            log.exception("delete_plan failed")
            show_message(self, "Could not delete the plan", str(exc))
            return
        self._changed()

    def set_active(self, plan: Plan, active: bool) -> None:
        try:
            plan_store.update_plan(self.ctx.conn, plan.id, active=active)
        except Exception as exc:
            log.exception("update_plan(active) failed")
            show_message(self, "Could not update the plan", str(exc))
        self._changed()

    def _changed(self) -> None:
        # Deferred: the emitting widget may be destroyed when the cards are rebuilt.
        QTimer.singleShot(0, self.ctx.plans_changed.emit)

    def apply_theme(self) -> None:
        """Called by the main window after a theme switch."""
        restyle_page(self)
        self.refresh()


def _plan_key(plan: Plan) -> tuple[object, ...]:
    return (
        plan.id,
        plan.name,
        plan.cap_bytes,
        plan.cycle_type,
        plan.cycle_value,
        plan.anchor_date,
        tuple(plan.thresholds),
        plan.active,
        tuple(plan.network_ids),
    )
