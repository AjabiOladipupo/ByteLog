"""Overview page, chart, sidebar status and tray helpers on an offscreen Qt platform."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("pyqtgraph")

from bytelog import timeutil  # noqa: E402
from bytelog.storage import settings  # noqa: E402
from bytelog.storage.db import write_tx  # noqa: E402
from bytelog.storage.models import Bucket, CollectorStatus  # noqa: E402
from bytelog.units import GB, MB, split_value_unit  # noqa: E402


def _make_app() -> object:
    """Create the widget QApplication while the test modules are being collected.

    A process can host only one Qt application object, and other test modules create a
    plain QGuiApplication (no widgets) when none exists. Creating ours at collection time,
    before any test runs, lets both kinds of test share one QApplication.
    """
    app = QtWidgets.QApplication.instance()
    if app is None:
        try:
            app = QtWidgets.QApplication(["bytelog-tests"])
        except Exception as exc:  # pragma: no cover - no usable Qt platform
            return exc
    return app


_APP = _make_app()


@pytest.fixture(scope="module")
def qapp():
    if not isinstance(_APP, QtWidgets.QApplication):
        pytest.skip(f"PySide6 cannot start a widget application: {_APP!r}")
    return _APP


class _InertPlatform:
    """Enough of the Platform protocol for the dashboard."""

    def __init__(self) -> None:
        self.launches = 0
        self.opened: list[object] = []

    def launch_collector(self) -> None:
        self.launches += 1

    def open_path(self, path: object) -> None:
        self.opened.append(path)

    def notify(self, title: str, message: str) -> bool:
        return True


def _platform() -> object:
    try:
        from bytelog.platform.fake import FakePlatform
    except ImportError:
        return _InertPlatform()
    return FakePlatform()


def _seed(conn: sqlite3.Connection) -> dict[str, int]:
    """Two networks; usage today (08:00-ish and 20:00-ish) and on the 1st of the month."""
    tz = timeutil.local_zone()
    today = datetime.now(tz).date()
    now = timeutil.now_utc_s()
    rows = {
        "today_rx": 300 * MB,
        "today_tx": 40 * MB,
        "early_rx": 2 * GB,
        "early_tx": 256 * MB,
    }
    with write_tx(conn):
        for i, (kind, identity, ssid) in enumerate(
            (("wifi", "ssid:4d69466921", "MiFi!"), ("ethernet", "{GUID-1}", None))
        ):
            conn.execute(
                "INSERT INTO networks(id, kind, identity, ssid, adapter_name, is_metered,"
                " first_seen_utc, last_seen_utc) VALUES(?,?,?,?,?,0,?,?)",
                (
                    i + 1,
                    kind,
                    identity,
                    ssid,
                    "Ethernet" if kind == "ethernet" else "Wi-Fi",
                    now,
                    now,
                ),
            )
        start = timeutil.local_midnight_utc_s(today, tz)
        bucket_8 = start + 8 * 3600
        bucket_20 = start + 20 * 3600
        conn.execute("INSERT INTO usage_15m VALUES(?,?,?,?)", (bucket_8, 1, 200 * MB, 30 * MB))
        conn.execute("INSERT INTO usage_15m VALUES(?,?,?,?)", (bucket_20, 2, 100 * MB, 10 * MB))
        conn.execute("INSERT INTO daily VALUES(?,?,?,?)", (today.isoformat(), 1, 200 * MB, 30 * MB))
        conn.execute("INSERT INTO daily VALUES(?,?,?,?)", (today.isoformat(), 2, 100 * MB, 10 * MB))
        first = today.replace(day=1)
        if first != today:
            conn.execute(
                "INSERT INTO daily VALUES(?,?,?,?)",
                (first.isoformat(), 1, rows["early_rx"], rows["early_tx"]),
            )
        else:
            rows["early_rx"] = rows["early_tx"] = 0
    return rows


@pytest.fixture
def window(qapp, db):
    from bytelog.ui.context import AppContext
    from bytelog.ui.main_window import MainWindow

    rows = _seed(db)
    ctx = AppContext(db, _platform())
    win = MainWindow(ctx)
    win.resize(1280, 820)
    yield win, rows
    win.quitting = True
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _card_text(card) -> str:
    return f"{card.value.text()} {card.unit.text()}"


def test_periods_drive_cards_and_chart(window) -> None:
    win, rows = window
    page = win.pages["overview"]
    assert type(page).__name__ == "OverviewPage"

    page.set_period("today", persist=False)
    today_total = rows["today_rx"] + rows["today_tx"]
    assert len(page.chart.buckets) == 12
    assert _card_text(page.total_card) == " ".join(split_value_unit(today_total))
    assert page.avg_card.caption.text() == "Busiest 2-hour slot"
    assert page.chart.tallest() is not None
    assert page.caption.text().startswith("Tallest bar: ")
    detail = page.down_card.detail.text()
    assert "% of total" in re.sub("<[^>]+>", "", detail)
    assert "IBM Plex Mono" in detail or "Consolas" in detail  # the number is set in mono

    page.set_period("month", persist=False)
    today = date.today()
    assert len(page.chart.buckets) == today.day
    month_total = today_total + rows["early_rx"] + rows["early_tx"]
    assert _card_text(page.total_card) == " ".join(split_value_unit(month_total))
    assert page.avg_card.caption.text() == "Daily average"

    page.set_period("all", persist=False)
    assert page.subtitle.text().startswith("Since ")


def test_period_choice_is_persisted(window, db) -> None:
    win, _ = window
    page = win.pages["overview"]
    page.set_period("week")
    assert settings.get(db, "overview_period") == "week"
    assert page.segmented.buttons["week"].isChecked()


def test_network_rows_and_plan_card(window, db) -> None:
    win, _ = window
    page = win.pages["overview"]
    page.set_period("today", persist=False)
    visible = [row for row in page.networks_card.rows if not row.isHidden()]
    assert len(visible) == 2
    assert page.plan_card.status is None  # empty state: "No data plan yet"

    from bytelog.storage import plans

    plans.create_plan(
        db, name="MiFi plan", cap_bytes=1 * GB, cycle_type="monthly_on_day", cycle_value=1
    )
    win.ctx.plans_changed.emit()
    status = page.plan_card.status
    assert status is not None
    assert page.plan_card.name.text() == "MiFi plan"
    assert "left" in page.plan_card.days.text()


def test_navigation_switches_pages(window) -> None:
    win, _ = window
    win.ctx.navigate.emit("plans")
    assert win.current == "plans"
    assert win.sidebar.buttons["plans"].isChecked()
    win.show_page("overview")
    assert win.stack.currentWidget() is win.pages["overview"]


def test_chart_axis_and_tooltip(qapp) -> None:
    from bytelog.ui.charts.stacked_bar import (
        StackedBarChart,
        nice_axis,
        tick_label,
        tooltip_html,
    )

    grid, unit, _ = nice_axis(int(6.3 * GB))
    assert unit == "GB" and grid[-1] >= 6.3 * GB and len(grid) <= 6
    grid, unit, divisor = nice_axis(0)
    assert unit == "MB" and grid[-1] > 0
    labels = [tick_label(v, unit, divisor) for v in grid]
    assert labels == ["0", "0.25 MB", "0.5 MB", "0.75 MB", "1 MB"]
    grid, unit, divisor = nice_axis(GB)
    assert [tick_label(v, unit, divisor) for v in grid][1] == "0.25 GB"

    day = date(2026, 10, 8)
    buckets = [
        Bucket("7", "Wednesday 7 Oct 2026", day - timedelta(days=1), 5 * MB, 1 * MB),
        Bucket("8", "Thursday 8 Oct 2026", day, 50 * MB, 2 * MB),
    ]
    chart = StackedBarChart()
    chart.set_buckets(buckets)
    assert chart.tallest() == buckets[1]
    html = tooltip_html(buckets[1])
    assert "Thursday 8 Oct 2026" in html and "52 MB" in html
    assert f"{52 * MB:,} bytes" in html  # exact value, not only the rounded one
    chart.set_buckets([])
    assert chart.tallest() is None


def test_collector_status_rules() -> None:
    from bytelog.ui.sidebar import describe_status

    now = 1_800_000_000
    running = CollectorStatus(1, "running", now - 100, now - 5, 10, "0.1.0", None)
    view = describe_status(running, now, 10, beat_stalled=False, starting=False)
    assert view.level == "running" and view.detail == "Last sample 5 seconds ago"

    old = CollectorStatus(1, "running", now - 1000, now - 61, 10, "0.1.0", None)
    assert describe_status(old, now, 10, False, False).level == "stopped"
    # A 30 s interval allows up to 90 s.
    assert describe_status(old, now, 30, False, False).level == "running"
    assert describe_status(running, now, 10, True, False).level == "stopped"
    assert describe_status(old, now, 10, False, True).level == "starting"

    paused = CollectorStatus(1, "paused", now - 100, now - 3, 10, "0.1.0", None)
    assert describe_status(paused, now, 10, False, False).title == "Collection paused"
    never = CollectorStatus(None, "stopped", None, None, 0, None, None)
    view = describe_status(never, now, 10, False, False)
    assert view.show_start and view.title == "Collector stopped"


def test_tray_tooltip_text() -> None:
    from bytelog.ui.tray import tooltip_text

    assert tooltip_text(340 * MB, False) == "ByteLog — Today: 340 MB"
    assert tooltip_text(0, True).endswith("(paused)")


def test_csv_export_runs_in_worker(window, tmp_path, monkeypatch, qapp) -> None:
    pytest.importorskip("bytelog.reports.csv_export")
    from PySide6.QtCore import QThreadPool

    win, _ = window
    page = win.pages["overview"]
    page.set_period("month", persist=False)
    target = tmp_path / "out.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    done: list[object] = []
    monkeypatch.setattr(page, "_export_done", done.append)
    monkeypatch.setattr(page, "_export_failed", done.append)
    page._export("csv")
    assert not page.csv_button.isEnabled()
    QThreadPool.globalInstance().waitForDone(10_000)
    for _ in range(20):
        qapp.processEvents()
        if done:
            break
    assert done == [target]
    assert target.is_file() and target.stat().st_size > 0
    assert page.csv_button.isEnabled() and page.csv_button.text() == "Export CSV"


def test_theme_switch_repaints(window) -> None:
    from bytelog.ui import theme

    win, _ = window
    try:
        win.ctx.theme_changed.emit("dark")
        assert theme.tokens() is theme.DARK
        assert "#0F1820" in QtWidgets.QApplication.instance().styleSheet()
        win.ctx.theme_changed.emit("light")
        assert theme.tokens() is theme.LIGHT
    finally:
        theme.set_theme("light")


def test_ensure_autostart_registers_only_when_frozen_and_wanted() -> None:
    from bytelog.platform.fake import FakePlatform
    from bytelog.ui.app import ensure_autostart

    platform = FakePlatform()
    platform.autostart = False
    assert ensure_autostart(platform, wanted=True, frozen=False) is None
    assert ensure_autostart(platform, wanted=False, frozen=True) is None
    assert platform.autostart is False
    thread = ensure_autostart(platform, wanted=True, frozen=True)
    assert thread is not None
    thread.join(5)
    assert platform.autostart is True


def test_mono_numbers_wraps_figures_only() -> None:
    from bytelog.ui.pages.overview import mono_numbers

    html = mono_numbers("19 days left · 348 MB/day allowance")
    assert re.sub("<[^>]+>", "", html) == "19 days left · 348 MB/day allowance"
    assert re.findall(r">([^<]+)</span>", html) == ["19", "348 MB"]
    assert re.findall(r">([^<]+)</span>", mono_numbers("Oct 5 – Nov 4")) == ["5", "4"]
    assert mono_numbers("a < b") == "a &lt; b"


def test_right_column_stacks_when_chart_would_be_squeezed(window) -> None:
    from PySide6.QtCore import Qt

    win, _ = window
    page = win.pages["overview"]
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.resize(1280, 820)
    win.show()
    QtWidgets.QApplication.processEvents()
    assert not page._stacked
    win.resize(960, 820)  # above ~900 px, but the chart would get under 460 px
    QtWidgets.QApplication.processEvents()
    assert page._stacked
    win.hide()


def test_tick_refreshes_visible_reports_page_once(window, monkeypatch) -> None:
    from PySide6.QtCore import Qt

    win, _ = window
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    win.show_page("reports")
    page = win.pages["reports"]
    calls: list[int] = []
    original = page.update_preview
    monkeypatch.setattr(page, "update_preview", lambda *a, **k: (calls.append(1), original())[1])
    win._tick()
    assert len(calls) == 1
    win.hide()
