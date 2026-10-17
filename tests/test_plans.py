from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from bytelog.storage import plans
from bytelog.storage.db import write_tx
from bytelog.storage.models import Cycle
from bytelog.units import GB

TODAY = date(2026, 10, 29)  # cycle Oct 5 - Nov 5 for day 5: 7 days left


def add_network(conn: sqlite3.Connection, ssid: str) -> int:
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO networks(kind, identity, ssid, first_seen_utc, last_seen_utc)"
            " VALUES('wifi', ?, ?, 0, 0)",
            (f"ssid:{ssid}", ssid),
        )
    return int(cur.lastrowid or 0)


def add_daily(conn: sqlite3.Connection, day: str, net: int, rx: int, tx: int = 0) -> None:
    with write_tx(conn):
        conn.execute("INSERT INTO daily VALUES(?, ?, ?, ?)", (day, net, rx, tx))


# --- thresholds -------------------------------------------------------------------------


def test_parse_thresholds() -> None:
    assert plans.parse_thresholds("80,100") == [80, 100]
    assert plans.parse_thresholds(" 100, 80% ;50 50 ") == [50, 80, 100]
    assert plans.parse_thresholds("1, 1000") == [1, 1000]
    assert plans.parse_thresholds("") == []


@pytest.mark.parametrize("text", ["0", "1001", "80,abc", "-5", "12.5", "80,,x"])
def test_parse_thresholds_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        plans.parse_thresholds(text)


def test_crossed_thresholds_integer_maths() -> None:
    cap = 30 * GB
    assert plans.crossed_thresholds(24 * GB, cap, [80, 100]) == [80]
    assert plans.crossed_thresholds(24 * GB - 1, cap, [80, 100]) == []
    assert plans.crossed_thresholds(31 * GB, cap, [100, 80]) == [80, 100]
    assert plans.crossed_thresholds(3, 3, [100]) == [100]


# --- CRUD -------------------------------------------------------------------------------


def test_create_get_list_delete(db: sqlite3.Connection) -> None:
    mifi = add_network(db, "MiFi")
    pid = plans.create_plan(
        db,
        name="  MiFi plan ",
        cap_bytes=30 * GB,
        cycle_type="monthly_on_day",
        cycle_value=5,
        thresholds="100,80",
        network_ids=[mifi],
    )
    other = plans.create_plan(
        db,
        name="Rolling",
        cap_bytes=GB,
        cycle_type="rolling_days",
        cycle_value=30,
        anchor_date=date(2026, 10, 1),
        active=False,
    )
    plan = plans.get_plan(db, pid)
    assert plan is not None
    assert plan.name == "MiFi plan"
    assert plan.thresholds == [80, 100]
    assert plan.network_ids == [mifi]
    assert plan.anchor_date is None
    assert [p.id for p in plans.list_plans(db)] == [pid, other]
    assert [p.id for p in plans.list_plans(db, active_only=True)] == [pid]
    rolling = plans.get_plan(db, other)
    assert rolling is not None
    assert rolling.anchor_date == date(2026, 10, 1)
    assert rolling.network_ids == []

    plans.delete_plan(db, pid)
    assert plans.get_plan(db, pid) is None
    assert db.execute("SELECT COUNT(*) FROM plan_networks").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": " "},
        {"cap_bytes": 0},
        {"cycle_value": 32},
        {"cycle_type": "rolling_days", "cycle_value": 30},  # no anchor
        {"thresholds": [0]},
        {"thresholds": "80,x"},
    ],
)
def test_create_rejects_bad_input(db: sqlite3.Connection, kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {
        "name": "P",
        "cap_bytes": GB,
        "cycle_type": "monthly_on_day",
        "cycle_value": 5,
    }
    args.update(kwargs)
    with pytest.raises(ValueError):
        plans.create_plan(db, **args)  # type: ignore[arg-type]
    assert plans.list_plans(db) == []


def test_update_plan_fields(db: sqlite3.Connection) -> None:
    a, b = add_network(db, "A"), add_network(db, "B")
    pid = plans.create_plan(
        db, name="P", cap_bytes=GB, cycle_type="monthly_on_day", cycle_value=5, network_ids=[a]
    )
    plans.update_plan(db, pid, name="Q", thresholds=[50], network_ids=[a, b], today=TODAY)
    plan = plans.get_plan(db, pid)
    assert plan is not None
    assert (plan.name, plan.cap_bytes, plan.thresholds, plan.network_ids) == ("Q", GB, [50], [a, b])

    plans.update_plan(
        db,
        pid,
        cycle_type="rolling_days",
        cycle_value=7,
        anchor_date=date(2026, 10, 1),
        network_ids=[],
        active=False,
        today=TODAY,
    )
    plan = plans.get_plan(db, pid)
    assert plan is not None
    assert (plan.cycle_type, plan.cycle_value, plan.anchor_date) == (
        "rolling_days",
        7,
        date(2026, 10, 1),
    )
    assert plan.network_ids == []
    assert not plan.active

    with pytest.raises(ValueError):
        plans.update_plan(db, pid, anchor_date=None, today=TODAY)
    with pytest.raises(KeyError):
        plans.update_plan(db, 999, name="x", today=TODAY)


def test_update_plan_clears_alerts_no_longer_crossed(db: sqlite3.Connection) -> None:
    net = add_network(db, "MiFi")
    add_daily(db, "2026-10-10", net, 25 * GB)
    pid = plans.create_plan(
        db, name="P", cap_bytes=30 * GB, cycle_type="monthly_on_day", cycle_value=5
    )
    with write_tx(db):
        db.executemany(
            "INSERT INTO alerts_sent VALUES(?, ?, ?, 0, 0, 1)",
            [(pid, "2026-10-05", 80), (pid, "2026-10-05", 50), (pid, "2026-09-05", 80)],
        )

    plans.update_plan(db, pid, cap_bytes=40 * GB, today=TODAY)  # 25/40 = 62.5%

    rows = db.execute("SELECT cycle_start, threshold FROM alerts_sent ORDER BY 1, 2").fetchall()
    assert [tuple(r) for r in rows] == [("2026-09-05", 80), ("2026-10-05", 50)]


# --- status -----------------------------------------------------------------------------


def test_plan_status(db: sqlite3.Connection) -> None:
    mifi, home = add_network(db, "MiFi"), add_network(db, "Home")
    add_daily(db, "2026-10-04", mifi, 5 * GB)  # previous cycle
    add_daily(db, "2026-10-05", mifi, 20 * GB, 2 * GB)
    add_daily(db, "2026-10-28", mifi, 2 * GB)
    add_daily(db, "2026-10-29", home, 9 * GB)  # not covered
    add_daily(db, "2026-11-05", mifi, 9 * GB)  # next cycle
    pid = plans.create_plan(
        db,
        name="MiFi plan",
        cap_bytes=30 * GB,
        cycle_type="monthly_on_day",
        cycle_value=5,
        network_ids=[mifi],
    )
    plan = plans.get_plan(db, pid)
    assert plan is not None

    status = plans.plan_status(db, plan, today=TODAY)

    assert status.cycle == Cycle(date(2026, 10, 5), date(2026, 11, 5))
    assert status.used == 24 * GB
    assert status.remaining == 6 * GB
    assert status.percent == pytest.approx(80.0)
    assert status.days_left == 7
    assert status.daily_allowance == 6 * GB // 7
    assert status.crossed == [80]
    assert status.over_by == 0


def test_plan_status_all_networks_and_over_cap(db: sqlite3.Connection) -> None:
    a, b = add_network(db, "A"), add_network(db, "B")
    add_daily(db, "2026-10-10", a, 20 * GB)
    add_daily(db, "2026-10-11", b, 13 * GB)
    pid = plans.create_plan(
        db, name="All", cap_bytes=30 * GB, cycle_type="monthly_on_day", cycle_value=5
    )
    plan = plans.get_plan(db, pid)
    assert plan is not None
    status = plans.plan_status(db, plan, today=date(2026, 11, 4))
    assert status.used == 33 * GB
    assert status.percent == pytest.approx(110.0)
    assert status.remaining == 0
    assert status.over_by == 3 * GB
    assert status.daily_allowance == 0
    assert status.days_left == 1
    assert status.crossed == [80, 100]


def test_plan_status_before_rolling_start(db: sqlite3.Connection) -> None:
    pid = plans.create_plan(
        db,
        name="Later",
        cap_bytes=GB,
        cycle_type="rolling_days",
        cycle_value=30,
        anchor_date=date(2026, 11, 1),
    )
    plan = plans.get_plan(db, pid)
    assert plan is not None
    status = plans.plan_status(db, plan, today=TODAY)
    assert status.cycle is None
    assert (status.used, status.remaining, status.days_left, status.crossed) == (0, GB, 0, [])
