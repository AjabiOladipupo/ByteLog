"""Offscreen tests for the secondary dashboard pages (networks, plans, reports, settings)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
repo = pytest.importorskip("bytelog.storage.repo")
plans = pytest.importorskip("bytelog.storage.plans")
pytest.importorskip("bytelog.storage.periods")
pytest.importorskip("bytelog.storage.maintenance")
fake = pytest.importorskip("bytelog.platform.fake")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bytelog.storage import settings  # noqa: E402
from bytelog.storage.db import write_tx  # noqa: E402
from bytelog.timeutil import now_utc_s  # noqa: E402
from bytelog.ui.context import AppContext  # noqa: E402
from bytelog.ui.pages._common import today_local  # noqa: E402
from bytelog.units import GB, MB, format_bytes  # noqa: E402

TODAY = today_local()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


def _seed(conn: sqlite3.Connection) -> None:
    now = now_utc_s()
    with write_tx(conn):
        conn.execute(
            "INSERT INTO networks(id, kind, identity, ssid, adapter_name, is_metered,"
            " first_seen_utc, last_seen_utc) VALUES"
            " (1, 'wifi', 'ssid:4d69466921', 'MTN MiFi', 'Wi-Fi', 1, ?, ?),"
            " (2, 'ethernet', '{ETH}', NULL, 'Ethernet', 0, ?, ?)",
            (now - 86400 * 40, now, now - 86400 * 40, now),
        )
        conn.execute(
            "INSERT INTO adapters(id, guid, name, description, kind, include_override,"
            " first_seen_utc, last_seen_utc) VALUES"
            " (1, '{WIFI}', 'Wi-Fi', 'Intel Wi-Fi 6', 'wifi', NULL, ?, ?),"
            " (2, '{VPN}', 'ProtonVPN', 'Wintun Userspace Tunnel', 'other', NULL, ?, ?)",
            (now, now, now, now),
        )
        for offset in range(3):
            day = (TODAY - timedelta(days=offset)).isoformat()
            conn.execute(
                "INSERT INTO daily(day_local, network_id, rx_bytes, tx_bytes) VALUES (?,1,?,?)",
                (day, 900 * MB, 100 * MB),
            )
            conn.execute(
                "INSERT INTO daily(day_local, network_id, rx_bytes, tx_bytes) VALUES (?,2,?,?)",
                (day, 200 * MB, 50 * MB),
            )
    plans.create_plan(
        conn,
        name="MiFi plan",
        cap_bytes=30 * GB,
        cycle_type="monthly_on_day",
        cycle_value=5,
        network_ids=[1],
    )


@pytest.fixture
def ctx(qapp: QApplication, db: sqlite3.Connection) -> AppContext:
    _seed(db)
    return AppContext(db, fake.FakePlatform())


def test_every_page_builds_and_refreshes(ctx: AppContext) -> None:
    from bytelog.ui.pages.networks import NetworksPage
    from bytelog.ui.pages.plans import PlansPage
    from bytelog.ui.pages.reports import ReportsPage
    from bytelog.ui.pages.settings import SettingsPage

    for cls in (NetworksPage, PlansPage, ReportsPage, SettingsPage):
        page = cls(ctx)
        assert page.objectName() == "Page"
        page.resize(1000, 800)
        page.refresh()
        page.refresh()
        page.apply_theme()
        page.grab()


def test_networks_rename_metered_and_override(ctx: AppContext) -> None:
    from bytelog.ui.pages.networks import NetworksPage

    page = NetworksPage(ctx)
    assert page.net_table.rowCount() == 2
    assert page.adapter_table.rowCount() == 2
    assert not page.location_card.isVisibleTo(page)

    row = next(
        r
        for r in range(page.net_table.rowCount())
        if page.net_table.item(r, 0).text() == "MTN MiFi"
    )
    page.net_table.item(row, 0).setText("Pocket router")
    assert repo.get_network(ctx.conn, 1).user_label == "Pocket router"
    page._net_signature = None
    page.refresh()
    row = next(
        r
        for r in range(page.net_table.rowCount())
        if page.net_table.item(r, 0).text() == "Pocket router"
    )
    page.net_table.item(row, 0).setText("")  # clearing restores the detected name
    assert repo.get_network(ctx.conn, 1).user_label is None

    page._set_metered(2, True)
    assert repo.get_network(ctx.conn, 2).is_metered

    vpn_row = next(
        r
        for r in range(page.adapter_table.rowCount())
        if page.adapter_table.item(r, 0).text() == "ProtonVPN"
    )
    assert page.adapter_table.cellWidget(vpn_row, 3).text() == "Excluded"
    page.adapter_table.cellWidget(vpn_row, 4).setCurrentIndex(1)  # Always include
    assert {a.name: a.include_override for a in repo.list_adapters(ctx.conn)}["ProtonVPN"] is True
    page.refresh()
    assert page.double_count_warning.isVisibleTo(page)


def test_networks_tick_updates_cells_without_rebuilding_widgets(ctx: AppContext) -> None:
    """Per-tick traffic must not recreate the checkboxes/combos under the user's cursor."""
    from bytelog.ui.pages.networks import NetworksPage

    page = NetworksPage(ctx)
    checkbox = page.net_table.cellWidget(0, 4)
    combo = page.adapter_table.cellWidget(0, 4)
    top_id = page.net_table.item(0, 0).data(0x0100)
    later = now_utc_s() + 120
    with write_tx(ctx.conn):  # what the collector does every tick
        ctx.conn.execute("UPDATE daily SET rx_bytes = rx_bytes + ? WHERE network_id = 2", (5 * GB,))
        ctx.conn.execute("UPDATE networks SET last_seen_utc = ?", (later,))
        ctx.conn.execute("UPDATE adapters SET last_seen_utc = ?", (later,))
    page.refresh()
    assert page.net_table.cellWidget(0, 4) is checkbox
    assert page.adapter_table.cellWidget(0, 4) is combo
    assert page.net_table.item(0, 0).data(0x0100) == top_id  # rows do not reorder
    row = next(r for r in range(2) if page.net_table.item(r, 0).data(0x0100) == 2)
    total = 3 * (250 * MB + 5 * GB)  # three seeded days
    assert page.net_table.item(row, 5).text() == format_bytes(total)

    repo.update_network(ctx.conn, 2, is_metered=True)  # a structural change does rebuild
    page.refresh()
    assert page.net_table.cellWidget(0, 4) is not checkbox


def test_location_hint_for_unknown_wifi(ctx: AppContext) -> None:
    from bytelog.ui.pages.networks import NetworksPage

    repo.upsert_network(ctx.conn, "wifi_unknown", "{WIFI}", None, "Wi-Fi", now_utc_s())
    page = NetworksPage(ctx)
    assert page.location_card.isVisibleTo(page)


def test_plan_editor_creates_and_updates(ctx: AppContext) -> None:
    from bytelog.ui.dialogs import PlanEditorDialog

    networks = repo.list_networks(ctx.conn)
    dialog = PlanEditorDialog(ctx.conn, networks, today=date(2026, 10, 1))
    dialog.name_edit.setText("Office")
    dialog.cap_spin.setValue(12.5)
    dialog.rolling_radio.setChecked(True)
    dialog.days_spin.setValue(30)
    dialog.thresholds_edit.setText("100, 50")
    dialog.network_list.item(1).setCheckState(dialog.network_list.item(1).checkState().Checked)
    dialog.accept()
    assert dialog.saved_plan_id is not None
    plan = plans.get_plan(ctx.conn, dialog.saved_plan_id)
    assert plan is not None
    assert plan.name == "Office"
    assert plan.cap_bytes == int(12.5 * GB)
    assert (plan.cycle_type, plan.cycle_value, plan.anchor_date) == (
        "rolling_days",
        30,
        date(2026, 10, 1),
    )
    assert plan.thresholds == [50, 100]
    assert len(plan.network_ids) == 1

    edit = PlanEditorDialog(ctx.conn, networks, plan)
    assert edit.rolling_radio.isChecked()
    assert edit.cap_spin.value() == pytest.approx(12.5)
    edit.monthly_radio.setChecked(True)
    edit.day_spin.setValue(31)
    edit.active_box.setChecked(False)
    edit.accept()
    updated = plans.get_plan(ctx.conn, plan.id)
    assert updated is not None
    assert (updated.cycle_type, updated.cycle_value, updated.active) == (
        "monthly_on_day",
        31,
        False,
    )


def test_plan_editor_rejects_bad_input(ctx: AppContext) -> None:
    from bytelog.ui.dialogs import PlanEditorDialog

    before = len(plans.list_plans(ctx.conn))
    dialog = PlanEditorDialog(ctx.conn, repo.list_networks(ctx.conn))
    dialog.accept()  # no name
    assert dialog.error_label.isVisibleTo(dialog)
    dialog.name_edit.setText("X")
    dialog.thresholds_edit.setText("80, lots")
    dialog.accept()
    assert "Alert thresholds" in dialog.error_label.text()
    assert dialog.saved_plan_id is None
    assert len(plans.list_plans(ctx.conn)) == before


def test_plans_page_cards_and_status(ctx: AppContext) -> None:
    from bytelog.ui.pages.plans import PlansPage, cycle_description

    page = PlansPage(ctx)
    assert len(page._cards) == 1
    card = page._cards[0]
    assert card.used.text() != "—"
    assert card.of_cap.text() == "of 30.0 GB"
    assert "Monthly, resets on day 5" in cycle_description(card.plan)
    page.set_active(card.plan, False)
    assert not plans.list_plans(ctx.conn)[0].active


def test_reports_preview_and_csv_export(ctx: AppContext, tmp_path: Path) -> None:
    from bytelog.ui.pages.reports import ReportsPage, default_export_name

    page = ReportsPage(ctx)
    page._set_range(TODAY - timedelta(days=2), TODAY)
    page.update_preview()
    assert page.model.rowCount() == 6
    shown = page.total_tile.value.text() + " " + page.total_tile.unit.text()
    assert shown == format_bytes(3 * 1250 * MB)

    page.network_combo.setCurrentIndex(page.network_combo.findData(2))
    page.update_preview()
    assert page.model.rowCount() == 3

    dest = tmp_path / default_export_name(TODAY, TODAY, "csv")
    page.export_to("csv", dest)
    QThreadPool.globalInstance().waitForDone(10_000)
    QApplication.processEvents()
    assert dest.is_file()
    assert page.open_button.isVisibleTo(page)
    page._open_last_export()
    assert ctx.platform.opened_paths == [dest]

    pdf = tmp_path / "report.pdf"
    page.export_to("pdf", pdf)
    QThreadPool.globalInstance().waitForDone(60_000)
    QApplication.processEvents()
    assert pdf.read_bytes().startswith(b"%PDF")
    assert page.export_label.text() == "Saved report.pdf"
    assert page.pdf_button.isEnabled()


def test_report_presets() -> None:
    from bytelog.ui.pages.reports import default_export_name, monthly_buckets, preset_range

    today = date(2026, 3, 17)
    assert preset_range("This month", today) == (date(2026, 3, 1), today)
    assert preset_range("Last month", today) == (date(2026, 2, 1), date(2026, 2, 28))
    assert preset_range("Last 30 days", today) == (date(2026, 2, 16), today)
    assert preset_range("This year", today) == (date(2026, 1, 1), today)
    assert default_export_name(date(2026, 10, 1), date(2026, 10, 17), "pdf") == (
        "ByteLog_2026-10-01_to_2026-10-17.pdf"
    )
    from bytelog.storage.models import Bucket

    days = [
        Bucket("", "", date(2026, 1, 31), 1, 2),
        Bucket("", "", date(2026, 2, 1), 3, 4),
        Bucket("", "", date(2026, 2, 2), 5, 6),
    ]
    months = monthly_buckets(days)
    assert [(b.label, b.rx, b.tx) for b in months] == [("Jan 26", 1, 2), ("Feb 26", 8, 10)]


def test_settings_writes_and_signals(ctx: AppContext) -> None:
    from bytelog.ui.pages.settings import SettingsPage

    keys: list[str] = []
    themes: list[str] = []
    ctx.settings_changed.connect(keys.append)
    ctx.theme_changed.connect(themes.append)
    page = SettingsPage(ctx)
    page.interval_spin.setValue(30)
    page.week_combo.setCurrentIndex(6)
    page.pause_box.setChecked(True)
    page.set_theme("dark")
    assert settings.sample_interval_s(ctx.conn) == 30
    assert settings.week_start(ctx.conn) == 6
    assert settings.get_bool(ctx.conn, "collection_paused")
    assert settings.get(ctx.conn, "theme") == "dark"
    assert keys[:3] == ["sample_interval_s", "week_start", "collection_paused"]
    assert themes == ["dark"]

    _settle_autostart(page)
    page.autostart_box.setChecked(True)
    _settle_autostart(page)
    assert ctx.platform.autostart is True
    assert settings.get_bool(ctx.conn, "start_at_login")


def test_settings_autostart_error_reverts(ctx: AppContext) -> None:
    from bytelog.ui.pages.settings import SettingsPage

    def broken(enabled: bool) -> None:
        raise OSError("schtasks failed")

    ctx.platform.set_autostart = broken  # type: ignore[method-assign]
    page = SettingsPage(ctx)
    _settle_autostart(page)
    page.autostart_box.setChecked(True)
    assert not page.autostart_box.isEnabled()  # disabled while schtasks runs off-thread
    _settle_autostart(page)
    assert not page.autostart_box.isChecked()
    assert page.autostart_box.isEnabled()
    assert "schtasks failed" in page.autostart_error.text()


def _settle_autostart(page, timeout_s: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while page.autostart_busy and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()
    assert not page.autostart_busy


def test_settings_autostart_rereads_after_background_registration(ctx: AppContext) -> None:
    """At first launch ensure_autostart registers the task after Settings was built."""
    from bytelog.ui.app import ensure_autostart
    from bytelog.ui.pages.settings import SettingsPage

    ctx.platform.autostart = False
    page = SettingsPage(ctx)
    _settle_autostart(page)
    assert not page.autostart_box.isChecked()
    thread = ensure_autostart(ctx.platform, True, True, page.reload_autostart_threadsafe)
    assert thread is not None
    thread.join(5)
    QApplication.processEvents()
    _settle_autostart(page)
    assert page.autostart_box.isChecked()


def test_settings_collector_controls(ctx: AppContext) -> None:
    from bytelog.ui.pages.settings import SettingsPage

    page = SettingsPage(ctx)
    assert page.collector_state(repo.read_collector_status(ctx.conn)) == "stopped"
    page.start_collector()
    assert ctx.platform.launches == 1
    assert page.status_label.text() == "Starting…"

    now = now_utc_s()
    with write_tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE collector_status SET state='running', heartbeat_utc=?, beat=3 WHERE id=1",
            (now,),
        )
    page.refresh()
    assert page.status_label.text().startswith("Collector running")
    page.request_stop()
    assert settings.get(ctx.conn, "stop_requested")
    assert ctx.platform.stop_event.is_set()

    with write_tx(ctx.conn):
        ctx.conn.execute("UPDATE collector_status SET heartbeat_utc=? WHERE id=1", (now - 3600,))
    assert page.collector_state(repo.read_collector_status(ctx.conn)) == "stale"


def test_settings_reset_direct_and_via_collector(ctx: AppContext, tmp_path: Path) -> None:
    from bytelog.ui.pages.settings import SettingsPage

    page = SettingsPage(ctx)
    assert page.backup_to(tmp_path / "backup.db")
    assert (tmp_path / "backup.db").stat().st_size > 0

    fired: list[str] = []
    ctx.data_changed.connect(lambda: fired.append("data"))
    page.reset_data(forget_networks=False)
    assert ctx.conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0
    assert len(repo.list_networks(ctx.conn)) == 2
    assert fired == ["data"]

    ctx.platform.lock_held = True
    page.reset_data(forget_networks=True)
    token = settings.get(ctx.conn, "reset_requested")
    assert token
    assert settings.get_bool(ctx.conn, "reset_forget_networks")
    assert page.reset_status.text().startswith("Resetting")
    settings.set_value(ctx.conn, "reset_done", token)
    page._poll_reset()
    assert page.reset_status.text().startswith("Done")
    assert fired == ["data", "data"]
