"""Modal dialogs: themed confirm/message boxes, the plan editor and the reset confirmation.

Dialogs use the ``Page`` object name so the application stylesheet gives them the page
background in both themes (a plain ``QMessageBox`` would keep the system palette).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from bytelog.storage.models import Network, Plan
from bytelog.ui import theme
from bytelog.ui.pages._common import input_stylesheet, pad_spin
from bytelog.ui.widgets import danger_button, muted_label, primary_button
from bytelog.units import GB, bytes_to_gb, gb_to_bytes

RESET_WORD = "RESET"


class _Dialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(input_stylesheet())
        self.setWindowTitle(title)
        self.setModal(True)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(22, 20, 22, 18)
        self.root.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("CardTitle")
        heading.setStyleSheet("font-size: 16px;")
        self.root.addWidget(heading)

    def _button_row(self, ok: QPushButton, cancel_text: str | None = "Cancel") -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        if cancel_text:
            cancel = QPushButton(cancel_text)
            cancel.clicked.connect(self.reject)
            row.addWidget(cancel)
        ok.setDefault(True)
        row.addWidget(ok)
        self.root.addSpacing(4)
        self.root.addLayout(row)
        return row


def _error_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.tokens().danger};")
    label.hide()
    return label


class ConfirmDialog(_Dialog):
    def __init__(
        self,
        title: str,
        text: str,
        confirm_text: str = "OK",
        *,
        danger: bool = False,
        cancel_text: str | None = "Cancel",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setMinimumWidth(380)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.root.addWidget(body)
        ok = danger_button(confirm_text) if danger else primary_button(confirm_text)
        ok.clicked.connect(self.accept)
        self._button_row(ok, cancel_text)


def confirm(
    parent: QWidget | None, title: str, text: str, confirm_text: str, *, danger: bool = False
) -> bool:
    dialog = ConfirmDialog(title, text, confirm_text, danger=danger, parent=parent)
    return dialog.exec() == QDialog.DialogCode.Accepted


def show_message(parent: QWidget | None, title: str, text: str) -> None:
    ConfirmDialog(title, text, "OK", cancel_text=None, parent=parent).exec()


class ResetDialog(_Dialog):
    """Asks the user to type RESET; exposes ``forget_networks`` after acceptance."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Reset all data", parent)
        text = QLabel(
            "This permanently deletes all recorded usage: samples, daily and 15-minute "
            "totals, and the record of sent alerts. Settings are kept. This cannot be undone."
        )
        text.setWordWrap(True)
        text.setMinimumWidth(400)
        self.root.addWidget(text)

        self.forget_box = QCheckBox("Also forget networks and plans")
        self.root.addWidget(self.forget_box)
        self.root.addWidget(
            muted_label("Network names, metered flags, adapter choices and data plans go too.")
        )

        self.root.addWidget(QLabel(f"Type {RESET_WORD} to confirm:"))
        self.word = QLineEdit()
        self.word.setPlaceholderText(RESET_WORD)
        self.root.addWidget(self.word)

        self.ok = danger_button("Reset all data")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.accept)
        self.word.textChanged.connect(lambda text: self.ok.setEnabled(text.strip() == RESET_WORD))
        self._button_row(self.ok)

    @property
    def forget_networks(self) -> bool:
        return self.forget_box.isChecked()

    def accept(self) -> None:
        if self.word.text().strip() == RESET_WORD:
            super().accept()


@dataclass(frozen=True)
class PlanFields:
    """What the editor produces; the keyword arguments of ``plans.create_plan``."""

    name: str
    cap_bytes: int
    cycle_type: str
    cycle_value: int
    anchor_date: date | None
    thresholds: list[int]
    network_ids: list[int]
    active: bool

    def as_kwargs(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cap_bytes": self.cap_bytes,
            "cycle_type": self.cycle_type,
            "cycle_value": self.cycle_value,
            "anchor_date": self.anchor_date,
            "thresholds": self.thresholds,
            "network_ids": self.network_ids,
            "active": self.active,
        }


def _qdate(day: date) -> QDate:
    return QDate(day.year, day.month, day.day)


def _pydate(qdate: QDate) -> date:
    return date(qdate.year(), qdate.month(), qdate.day())


class PlanEditorDialog(_Dialog):
    """Create or edit a data plan. ``accept()`` validates and saves through storage.plans."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        networks: list[Network],
        plan: Plan | None = None,
        *,
        default_thresholds: str = "80,100",
        today: date | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Edit plan" if plan else "New plan", parent)
        self.conn = conn
        self.plan = plan
        self.saved_plan_id: int | None = None
        today = today or date.today()
        self.setMinimumWidth(500)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        row = 0

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. MiFi plan")
        self.name_edit.setMaxLength(60)
        grid.addWidget(QLabel("Name"), row, 0)
        grid.addWidget(self.name_edit, row, 1)
        row += 1

        self.cap_spin = QDoubleSpinBox()
        self.cap_spin.setRange(0.1, 1_000_000.0)
        self.cap_spin.setDecimals(1)
        self.cap_spin.setSingleStep(1.0)
        self.cap_spin.setSuffix(" GB")
        self.cap_spin.setValue(30.0)
        self.cap_spin.setFixedWidth(150)
        pad_spin(self.cap_spin)
        cap_row = QHBoxLayout()
        cap_row.addWidget(self.cap_spin)
        cap_row.addWidget(muted_label("GB = 1024³ bytes (1 GB = 1024 MB)"), 1)
        grid.addWidget(QLabel("Data cap"), row, 0)
        grid.addLayout(cap_row, row, 1)
        row += 1

        self.monthly_radio = QRadioButton("Monthly, resets on day")
        self.rolling_radio = QRadioButton("Every")
        self.cycle_group = QButtonGroup(self)
        self.cycle_group.addButton(self.monthly_radio)
        self.cycle_group.addButton(self.rolling_radio)
        self.day_spin = QSpinBox()
        self.day_spin.setRange(1, 31)
        self.day_spin.setValue(1)
        self.day_spin.setFixedWidth(80)
        pad_spin(self.day_spin)
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 366)
        self.days_spin.setValue(30)
        self.days_spin.setSuffix(" days")
        self.days_spin.setFixedWidth(110)
        pad_spin(self.days_spin)
        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("d MMM yyyy")
        self.start_edit.setDate(_qdate(today))

        cycle_box = QVBoxLayout()
        cycle_box.setSpacing(6)
        monthly = QHBoxLayout()
        monthly.addWidget(self.monthly_radio)
        monthly.addWidget(self.day_spin)
        monthly.addStretch(1)
        cycle_box.addLayout(monthly)
        short_note = muted_label("Months without that day reset on their last day.")
        short_note.setContentsMargins(24, 0, 0, 4)
        cycle_box.addWidget(short_note)
        rolling = QHBoxLayout()
        rolling.addWidget(self.rolling_radio)
        rolling.addWidget(self.days_spin)
        rolling.addWidget(QLabel("starting"))
        rolling.addWidget(self.start_edit)
        rolling.addStretch(1)
        cycle_box.addLayout(rolling)
        cycle_label = QLabel("Billing cycle")
        cycle_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        cycle_label.setContentsMargins(0, 6, 0, 0)
        grid.addWidget(cycle_label, row, 0)
        grid.addLayout(cycle_box, row, 1)
        row += 1

        self.network_list = QListWidget()
        for network in networks:
            item = QListWidgetItem(network.label)
            item.setData(Qt.ItemDataRole.UserRole, network.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.network_list.addItem(item)
        visible = max(2, min(len(networks), 5))
        self.network_list.setFixedHeight(visible * 26 + 8)
        nets_box = QVBoxLayout()
        nets_box.setSpacing(4)
        if networks:
            nets_box.addWidget(self.network_list)
        else:
            self.network_list.hide()
        nets_box.addWidget(
            muted_label("Leave all unchecked to count every network.")
            if networks
            else muted_label("All networks (none detected yet).")
        )
        nets_label = QLabel("Networks")
        nets_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        nets_label.setContentsMargins(0, 6, 0, 0)
        grid.addWidget(nets_label, row, 0)
        grid.addLayout(nets_box, row, 1)
        row += 1

        self.thresholds_edit = QLineEdit(default_thresholds)
        self.thresholds_edit.setPlaceholderText("80,100")
        self.thresholds_edit.setFixedWidth(150)
        thr_row = QHBoxLayout()
        thr_row.addWidget(self.thresholds_edit)
        thr_row.addWidget(muted_label("% of the cap, comma-separated"), 1)
        grid.addWidget(QLabel("Alerts at"), row, 0)
        grid.addLayout(thr_row, row, 1)
        row += 1

        self.active_box = QCheckBox("Active (track usage and send alerts)")
        self.active_box.setChecked(True)
        grid.addWidget(self.active_box, row, 1)

        self.root.addLayout(grid)
        self.error_label = _error_label()
        self.root.addWidget(self.error_label)

        self.save_button = primary_button("Save plan" if plan else "Create plan")
        self.save_button.clicked.connect(self.accept)
        self._button_row(self.save_button)

        self.monthly_radio.toggled.connect(self._sync_cycle_controls)
        self.monthly_radio.setChecked(True)
        if plan:
            self._load(plan)
        self._sync_cycle_controls()

    def _load(self, plan: Plan) -> None:
        self.name_edit.setText(plan.name)
        self.cap_spin.setValue(round(bytes_to_gb(plan.cap_bytes), 1))
        if plan.cycle_type == "rolling_days":
            self.rolling_radio.setChecked(True)
            self.days_spin.setValue(plan.cycle_value)
            if plan.anchor_date:
                self.start_edit.setDate(_qdate(plan.anchor_date))
        else:
            self.monthly_radio.setChecked(True)
            self.day_spin.setValue(plan.cycle_value)
        self.thresholds_edit.setText(",".join(str(t) for t in plan.thresholds))
        self.active_box.setChecked(plan.active)
        chosen = set(plan.network_ids)
        for i in range(self.network_list.count()):
            item = self.network_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in chosen:
                item.setCheckState(Qt.CheckState.Checked)

    def _sync_cycle_controls(self) -> None:
        monthly = self.monthly_radio.isChecked()
        self.day_spin.setEnabled(monthly)
        self.days_spin.setEnabled(not monthly)
        self.start_edit.setEnabled(not monthly)

    def _checked_network_ids(self) -> list[int]:
        ids: list[int] = []
        for i in range(self.network_list.count()):
            item = self.network_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return ids

    def fields(self) -> PlanFields:
        """Validated form values. Raises ValueError with a user-facing message."""
        from bytelog.storage import plans as plan_store

        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("Give the plan a name.")
        cap_bytes = gb_to_bytes(self.cap_spin.value())
        if cap_bytes < GB // 10:
            raise ValueError("The cap must be at least 0.1 GB.")
        try:
            thresholds = plan_store.parse_thresholds(self.thresholds_edit.text())
        except ValueError as exc:
            raise ValueError(f"Alert thresholds: {exc}") from exc
        if self.monthly_radio.isChecked():
            cycle_type, cycle_value, anchor = "monthly_on_day", self.day_spin.value(), None
        else:
            cycle_type, cycle_value = "rolling_days", self.days_spin.value()
            anchor = _pydate(self.start_edit.date())
        return PlanFields(
            name=name,
            cap_bytes=cap_bytes,
            cycle_type=cycle_type,
            cycle_value=cycle_value,
            anchor_date=anchor,
            thresholds=thresholds,
            network_ids=self._checked_network_ids(),
            active=self.active_box.isChecked(),
        )

    def save(self) -> int:
        """Validate and write the plan; returns its id. Raises ValueError on bad input."""
        from bytelog.storage import plans as plan_store

        fields = self.fields()
        if self.plan is None:
            return plan_store.create_plan(self.conn, **fields.as_kwargs())  # type: ignore[arg-type]
        plan_store.update_plan(self.conn, self.plan.id, **fields.as_kwargs())
        return self.plan.id

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def accept(self) -> None:
        try:
            self.saved_plan_id = self.save()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        except sqlite3.Error as exc:
            self.show_error(f"Could not save the plan: {exc}")
            return
        super().accept()
