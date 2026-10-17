from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bytelog.storage import settings
from bytelog.storage.db import write_tx
from bytelog.storage.models import Period
from bytelog.storage.periods import (
    daily_rows,
    daily_totals,
    date_range_label,
    network_usage,
    period_range,
    summarize,
    today_total,
)

SAT = date(2026, 10, 17)


def add_network(conn: sqlite3.Connection, ssid: str, label: str | None = None) -> int:
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO networks(kind, identity, ssid, user_label, first_seen_utc, last_seen_utc)"
            " VALUES('wifi', ?, ?, ?, 0, 0)",
            (f"ssid:{ssid}", ssid, label),
        )
    return int(cur.lastrowid or 0)


def add_daily(conn: sqlite3.Connection, day: date, net: int, rx: int, tx: int = 0) -> None:
    with write_tx(conn):
        conn.execute(
            "INSERT INTO daily VALUES(?, ?, ?, ?) ON CONFLICT DO UPDATE SET"
            " rx_bytes=rx_bytes+excluded.rx_bytes, tx_bytes=tx_bytes+excluded.tx_bytes",
            (day.isoformat(), net, rx, tx),
        )


def add_fine(conn: sqlite3.Connection, bucket_utc: int, net: int, rx: int, tx: int = 0) -> None:
    assert bucket_utc % 900 == 0
    with write_tx(conn):
        conn.execute("INSERT INTO usage_15m VALUES(?, ?, ?, ?)", (bucket_utc, net, rx, tx))


def utc(tz: ZoneInfo, *args: int) -> int:
    return int(datetime(*args, tzinfo=tz).timestamp())  # type: ignore[misc]


@pytest.fixture
def net(db: sqlite3.Connection) -> int:
    return add_network(db, "Home")


# --- ranges and labels ------------------------------------------------------------------


def test_period_range_week_start() -> None:
    assert period_range(Period.WEEK, SAT, 0) == (date(2026, 10, 12), SAT)  # Monday
    assert period_range(Period.WEEK, SAT, 6) == (date(2026, 10, 11), SAT)  # Sunday
    assert period_range(Period.WEEK, SAT, 5) == (SAT, SAT)  # Saturday = today
    monday = date(2026, 10, 12)
    assert period_range(Period.WEEK, monday, 0) == (monday, monday)
    assert period_range(Period.WEEK, monday, 1) == (date(2026, 10, 6), monday)


def test_period_range_month_and_today() -> None:
    assert period_range(Period.MONTH, SAT, 0) == (date(2026, 10, 1), SAT)
    assert period_range(Period.MONTH, date(2026, 10, 1), 0) == (date(2026, 10, 1),) * 2
    assert period_range(Period.TODAY, SAT, 0) == (SAT, SAT)
    assert period_range("today", SAT, 0) == (SAT, SAT)  # type: ignore[arg-type]


def test_date_range_label() -> None:
    assert date_range_label(SAT, SAT) == "Saturday 17 October 2026"
    assert date_range_label(date(2026, 10, 12), SAT) == "Mon 12 Oct – Sat 17 Oct 2026"
    assert (
        date_range_label(date(2025, 12, 29), date(2026, 1, 2)) == "Mon 29 Dec 2025 – Fri 2 Jan 2026"
    )
    assert date_range_label(date(2026, 3, 10), SAT, Period.ALL) == "Since March 2026"


# --- Today ------------------------------------------------------------------------------


def test_today_lagos(db: sqlite3.Connection, net: int, lagos: ZoneInfo) -> None:
    add_fine(db, utc(lagos, 2026, 10, 17, 8, 15), net, 100, 10)
    add_fine(db, utc(lagos, 2026, 10, 17, 9, 45), net, 50, 5)
    add_fine(db, utc(lagos, 2026, 10, 17, 18, 0), net, 300, 30)
    add_fine(db, utc(lagos, 2026, 10, 16, 23, 45), net, 999)  # yesterday
    add_fine(db, utc(lagos, 2026, 10, 18, 0, 0), net, 999)  # tomorrow
    add_daily(db, SAT, net, 450, 45)

    s = summarize(db, Period.TODAY, today=SAT, tz=lagos, week_start=0)

    assert [b.label for b in s.buckets] == [f"{h:02d}" for h in range(0, 24, 2)]
    assert s.buckets[4].tooltip_title == "08:00–10:00"
    assert s.buckets[11].tooltip_title == "22:00–24:00"
    assert (s.buckets[4].rx, s.buckets[4].tx) == (150, 15)
    assert s.buckets[9].total == 330
    assert sum(b.total for b in s.buckets) == 495
    assert (s.rx, s.tx) == (450, 45)  # cards come from daily
    assert s.average_title == "Busiest 2-hour slot"
    assert s.average_bytes == 330
    assert s.average_detail == "18:00–20:00"
    assert s.start == s.end == SAT
    assert s.buckets[4].start == datetime(2026, 10, 17, 8, tzinfo=lagos)


def test_today_empty(db: sqlite3.Connection, lagos: ZoneInfo) -> None:
    s = summarize(db, Period.TODAY, today=SAT, tz=lagos, week_start=0)
    assert len(s.buckets) == 12
    assert s.total == 0
    assert s.average_bytes == 0
    assert s.per_network == []


@pytest.mark.parametrize(
    ("zone", "edges"),
    [
        # (UTC hour, minute) of a bucket -> expected slot; 18:15 UTC is the previous local day.
        ("Asia/Kolkata", [((18, 15), None), ((18, 30), 0), ((20, 15), 0), ((20, 30), 1)]),
        ("Asia/Kathmandu", [((18, 0), None), ((18, 15), 0), ((20, 0), 0), ((20, 15), 1)]),
    ],
)
def test_today_fractional_offsets(
    db: sqlite3.Connection, net: int, zone: str, edges: list[tuple[tuple[int, int], int | None]]
) -> None:
    tz = ZoneInfo(zone)
    base = int(datetime(2026, 10, 16, tzinfo=ZoneInfo("UTC")).timestamp())
    expected = [0] * 12
    for i, ((hour, minute), slot) in enumerate(edges):
        amount = 10**i
        add_fine(db, base + hour * 3600 + minute * 60, net, amount)
        if slot is not None:
            expected[slot] += amount

    s = summarize(db, Period.TODAY, today=SAT, tz=tz, week_start=0)

    assert [b.rx for b in s.buckets] == expected


def fill_day(conn: sqlite3.Connection, net: int, day: date, tz: ZoneInfo) -> int:
    lo = utc(tz, day.year, day.month, day.day)
    nxt = day + timedelta(days=1)
    hi = utc(tz, nxt.year, nxt.month, nxt.day)
    with write_tx(conn):
        conn.executemany(
            "INSERT INTO usage_15m VALUES(?, ?, 1, 0)", [(b, net) for b in range(lo, hi, 900)]
        )
    return (hi - lo) // 900


def test_today_london_fall_back(db: sqlite3.Connection, net: int) -> None:
    london = ZoneInfo("Europe/London")
    day = date(2026, 10, 25)  # 25-hour day: 01:00-02:00 happens twice
    assert fill_day(db, net, day, london) == 100
    s = summarize(db, Period.TODAY, today=day, tz=london, week_start=0)
    assert [b.rx for b in s.buckets] == [12] + [8] * 11


def test_today_london_spring_forward(db: sqlite3.Connection, net: int) -> None:
    london = ZoneInfo("Europe/London")
    day = date(2026, 3, 29)  # 23-hour day: 01:00-02:00 does not exist
    assert fill_day(db, net, day, london) == 92
    s = summarize(db, Period.TODAY, today=day, tz=london, week_start=0)
    assert [b.rx for b in s.buckets] == [4] + [8] * 11


# --- Week / Month -----------------------------------------------------------------------


def test_week_monday_vs_sunday(db: sqlite3.Connection, net: int, lagos: ZoneInfo) -> None:
    add_daily(db, date(2026, 10, 11), net, 700)  # Sunday
    add_daily(db, date(2026, 10, 12), net, 100, 20)  # Monday
    add_daily(db, date(2026, 10, 16), net, 200)
    add_daily(db, date(2026, 10, 18), net, 5000)  # future (clock trouble): ignored

    mon = summarize(db, Period.WEEK, today=SAT, tz=lagos, week_start=0)
    assert [b.label for b in mon.buckets] == [
        "Mon 12",
        "Tue 13",
        "Wed 14",
        "Thu 15",
        "Fri 16",
        "Sat 17",
    ]
    assert mon.buckets[0].tooltip_title == "Monday 12 Oct 2026"
    assert [b.total for b in mon.buckets] == [120, 0, 0, 0, 200, 0]
    assert (mon.rx, mon.tx) == (300, 20)
    assert mon.average_title == "Daily average"
    assert mon.average_bytes == 320 // 6
    assert mon.average_detail == "over 6 days"
    assert (mon.start, mon.end) == (date(2026, 10, 12), SAT)

    sun = summarize(db, Period.WEEK, today=SAT, tz=lagos, week_start=6)
    assert sun.buckets[0].label == "Sun 11"
    assert len(sun.buckets) == 7
    assert sun.total == 1020
    assert sun.average_detail == "over 7 days"


def test_week_start_defaults_to_setting(db: sqlite3.Connection, lagos: ZoneInfo) -> None:
    settings.set_value(db, "week_start", 6)
    s = summarize(db, Period.WEEK, today=SAT, tz=lagos)
    assert s.start == date(2026, 10, 11)


def test_month(db: sqlite3.Connection, net: int, lagos: ZoneInfo) -> None:
    add_daily(db, date(2026, 9, 30), net, 999)
    add_daily(db, date(2026, 10, 1), net, 10)
    add_daily(db, SAT, net, 7)
    s = summarize(db, Period.MONTH, today=SAT, tz=lagos, week_start=0)
    assert [b.label for b in s.buckets] == [str(d) for d in range(1, 18)]
    assert s.total == 17
    assert s.average_bytes == 1
    assert s.average_detail == "over 17 days"

    first = summarize(db, Period.MONTH, today=date(2026, 10, 1), tz=lagos, week_start=0)
    assert len(first.buckets) == 1
    assert first.average_detail == "over 1 day"


# --- All time ---------------------------------------------------------------------------


def test_all_time_months(db: sqlite3.Connection, net: int, lagos: ZoneInfo) -> None:
    add_daily(db, date(2026, 3, 10), net, 100)
    add_daily(db, date(2026, 3, 20), net, 60, 4)
    add_daily(db, date(2026, 5, 2), net, 36)
    add_daily(db, SAT, net, 0, 0)  # empty row does not move the start
    s = summarize(db, Period.ALL, today=SAT, tz=lagos, week_start=0)

    assert [b.label for b in s.buckets] == [
        "Mar 26",
        "Apr 26",
        "May 26",
        "Jun 26",
        "Jul 26",
        "Aug 26",
        "Sep 26",
        "Oct 26",
    ]
    assert s.buckets[0].tooltip_title == "March 2026"
    assert s.buckets[0].start == date(2026, 3, 1)
    assert [b.total for b in s.buckets][:3] == [164, 0, 36]
    assert s.total == 200
    assert s.average_title == "Monthly average"
    assert s.average_bytes == 25
    assert s.average_detail == "over 8 months"
    assert (s.start, s.end) == (date(2026, 3, 10), SAT)


def test_all_time_across_years(db: sqlite3.Connection, net: int, lagos: ZoneInfo) -> None:
    add_daily(db, date(2025, 11, 30), net, 1)
    s = summarize(db, Period.ALL, today=date(2026, 1, 5), tz=lagos, week_start=0)
    assert [b.label for b in s.buckets] == ["Nov 25", "Dec 25", "Jan 26"]


def test_all_time_empty_database(db: sqlite3.Connection, lagos: ZoneInfo) -> None:
    s = summarize(db, Period.ALL, today=SAT, tz=lagos, week_start=0)
    assert [b.label for b in s.buckets] == ["Oct 26"]
    assert s.start == date(2026, 10, 1)
    assert s.average_detail == "over 1 month"
    assert date_range_label(s.start, s.end, Period.ALL) == "Since October 2026"


# --- per network, filters, rows ---------------------------------------------------------


def test_per_network_and_filter(db: sqlite3.Connection, net: int, lagos: ZoneInfo) -> None:
    mifi = add_network(db, "MiFi", label="MTN MiFi")
    add_daily(db, SAT, net, 100)
    add_daily(db, SAT, mifi, 300, 50)
    add_daily(db, date(2026, 10, 1), mifi, 1000)

    s = summarize(db, Period.WEEK, today=SAT, tz=lagos, week_start=0)
    assert [(u.label, u.total) for u in s.per_network] == [("MTN MiFi", 350), ("Home", 100)]

    only = summarize(db, Period.WEEK, today=SAT, tz=lagos, week_start=0, network_ids=[net])
    assert only.total == 100
    assert [u.network_id for u in only.per_network] == [net]

    usage = network_usage(db, date(2026, 10, 1), SAT)
    assert [(u.label, u.rx, u.tx) for u in usage] == [("MTN MiFi", 1300, 50), ("Home", 100, 0)]


def test_daily_rows_and_totals(db: sqlite3.Connection, net: int) -> None:
    mifi = add_network(db, "MiFi")
    add_daily(db, date(2026, 10, 15), mifi, 5, 1)
    add_daily(db, date(2026, 10, 15), net, 7)
    add_daily(db, SAT, net, 3)

    rows = daily_rows(db, date(2026, 10, 15), SAT)
    assert [(r.day.day, r.network_label, r.total) for r in rows] == [
        (15, "Home", 7),
        (15, "MiFi", 6),
        (17, "Home", 3),
    ]
    assert len(daily_rows(db, date(2026, 10, 15), SAT, [mifi])) == 1

    totals = daily_totals(db, date(2026, 10, 15), SAT)
    assert [(b.label, b.total) for b in totals] == [("15 Oct", 13), ("16 Oct", 0), ("17 Oct", 3)]
    assert totals[0].start == date(2026, 10, 15)


def test_today_total(db: sqlite3.Connection, net: int, lagos: ZoneInfo) -> None:
    today = datetime.now(lagos).date()
    add_daily(db, today, net, 40, 2)
    add_daily(db, today - timedelta(days=1), net, 1000)
    assert today_total(db, lagos) == 42
