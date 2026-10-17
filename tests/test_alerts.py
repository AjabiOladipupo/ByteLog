from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bytelog.collector.alerts import check_alerts, format_alert
from bytelog.storage import plans
from bytelog.storage.db import write_tx
from bytelog.units import GB, MB

LAGOS = ZoneInfo("Africa/Lagos")
NOW = int(datetime(2026, 10, 29, 12, 0, tzinfo=LAGOS).timestamp())  # 7 days left (day 5)


class FakeNotifier:
    def __init__(self, result: bool = True, error: bool = False) -> None:
        self.result = result
        self.error = error
        self.notifications: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> bool:
        self.notifications.append((title, message))
        if self.error:
            raise OSError("toast service unavailable")
        return self.result


def setup_plan(conn: sqlite3.Connection, thresholds: str = "80,100") -> tuple[int, int]:
    with write_tx(conn):
        net = conn.execute(
            "INSERT INTO networks(kind, identity, ssid, first_seen_utc, last_seen_utc)"
            " VALUES('wifi', 'ssid:4d69', 'MiFi', 0, 0)"
        ).lastrowid
    pid = plans.create_plan(
        conn,
        name="MiFi plan",
        cap_bytes=30 * GB,
        cycle_type="monthly_on_day",
        cycle_value=5,
        thresholds=thresholds,
        network_ids=[int(net or 0)],
    )
    return pid, int(net or 0)


def use(conn: sqlite3.Connection, net: int, day: str, amount: int) -> None:
    with write_tx(conn):
        conn.execute(
            "INSERT INTO daily VALUES(?, ?, ?, 0) ON CONFLICT DO UPDATE"
            " SET rx_bytes = rx_bytes + excluded.rx_bytes",
            (day, net, amount),
        )


def sent(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
    rows = conn.execute("SELECT cycle_start, threshold, shown FROM alerts_sent ORDER BY 1, 2")
    return [tuple(r) for r in rows]  # type: ignore[misc]


def test_format_alert() -> None:
    assert (
        format_alert("MiFi plan", 80, 24 * GB, 30 * GB, 7)
        == "MiFi plan: 80% used (24.0 of 30.0 GB), 7 days left."
    )
    assert (
        format_alert("MiFi plan", 100, int(30.2 * GB), 30 * GB, 1)
        == "MiFi plan: 100% used (30.2 of 30.0 GB), 1 day left."
    )
    assert format_alert("Small", 80, 400 * MB, 500 * MB, 3) == (
        "Small: 80% used (400 MB of 500 MB), 3 days left."
    )


def test_nothing_crossed_sends_nothing(db: sqlite3.Connection) -> None:
    _, net = setup_plan(db)
    use(db, net, "2026-10-10", 23 * GB)
    notifier = FakeNotifier()
    assert check_alerts(db, notifier, NOW, LAGOS) == []  # type: ignore[arg-type]
    assert notifier.notifications == []
    assert sent(db) == []


def test_alert_is_sent_once(db: sqlite3.Connection) -> None:
    _, net = setup_plan(db)
    use(db, net, "2026-10-10", 24 * GB)
    notifier = FakeNotifier()

    shown = check_alerts(db, notifier, NOW, LAGOS)  # type: ignore[arg-type]
    assert shown == ["MiFi plan: 80% used (24.0 of 30.0 GB), 7 days left."]
    assert notifier.notifications == [("ByteLog", shown[0])]
    assert sent(db) == [("2026-10-05", 80, 1)]

    assert check_alerts(db, notifier, NOW + 10, LAGOS) == []  # type: ignore[arg-type]
    assert len(notifier.notifications) == 1


def test_several_thresholds_at_once_give_one_toast(db: sqlite3.Connection) -> None:
    _, net = setup_plan(db, "50,80,100")
    use(db, net, "2026-10-10", int(30.2 * GB))
    notifier = FakeNotifier()

    shown = check_alerts(db, notifier, NOW, LAGOS)  # type: ignore[arg-type]

    assert shown == ["MiFi plan: 100% used (30.2 of 30.0 GB), 7 days left."]
    assert len(notifier.notifications) == 1
    assert sent(db) == [("2026-10-05", 50, 1), ("2026-10-05", 80, 1), ("2026-10-05", 100, 1)]


def test_next_threshold_fires_later(db: sqlite3.Connection) -> None:
    _, net = setup_plan(db)
    use(db, net, "2026-10-10", 24 * GB)
    notifier = FakeNotifier()
    check_alerts(db, notifier, NOW, LAGOS)  # type: ignore[arg-type]
    use(db, net, "2026-10-29", 6 * GB)
    shown = check_alerts(db, notifier, NOW + 60, LAGOS)  # type: ignore[arg-type]
    assert shown == ["MiFi plan: 100% used (30.0 of 30.0 GB), 7 days left."]
    assert len(notifier.notifications) == 2


def test_raised_cap_rearms(db: sqlite3.Connection) -> None:
    pid, net = setup_plan(db)
    use(db, net, "2026-10-10", 24 * GB)
    notifier = FakeNotifier()
    check_alerts(db, notifier, NOW, LAGOS)  # type: ignore[arg-type]

    plans.update_plan(db, pid, cap_bytes=40 * GB, today=datetime.fromtimestamp(NOW, LAGOS).date())
    assert sent(db) == []
    assert check_alerts(db, notifier, NOW, LAGOS) == []  # type: ignore[arg-type]

    use(db, net, "2026-10-29", 8 * GB)  # 32 of 40 GB
    shown = check_alerts(db, notifier, NOW, LAGOS)  # type: ignore[arg-type]
    assert shown == ["MiFi plan: 80% used (32.0 of 40.0 GB), 7 days left."]


def test_new_cycle_rearms(db: sqlite3.Connection) -> None:
    _, net = setup_plan(db)
    use(db, net, "2026-10-10", 24 * GB)
    notifier = FakeNotifier()
    check_alerts(db, notifier, NOW, LAGOS)  # type: ignore[arg-type]

    use(db, net, "2026-11-06", 25 * GB)
    later = int(datetime(2026, 11, 6, 9, 0, tzinfo=LAGOS).timestamp())
    shown = check_alerts(db, notifier, later, LAGOS)  # type: ignore[arg-type]

    assert shown == ["MiFi plan: 80% used (25.0 of 30.0 GB), 29 days left."]
    assert sent(db) == [("2026-10-05", 80, 1), ("2026-11-05", 80, 1)]


@pytest.mark.parametrize("notifier", [FakeNotifier(result=False), FakeNotifier(error=True)])
def test_notify_failure_is_recorded_not_retried(
    db: sqlite3.Connection, notifier: FakeNotifier
) -> None:
    _, net = setup_plan(db)
    use(db, net, "2026-10-10", 25 * GB)

    assert check_alerts(db, notifier, NOW, LAGOS) == []  # type: ignore[arg-type]
    assert sent(db) == [("2026-10-05", 80, 0)]
    assert check_alerts(db, notifier, NOW + 10, LAGOS) == []  # type: ignore[arg-type]
    assert len(notifier.notifications) == 1


def test_inactive_plans_are_ignored(db: sqlite3.Connection) -> None:
    pid, net = setup_plan(db)
    plans.update_plan(db, pid, active=False)
    use(db, net, "2026-10-10", 29 * GB)
    notifier = FakeNotifier()
    assert check_alerts(db, notifier, NOW, LAGOS) == []  # type: ignore[arg-type]
    assert sent(db) == []
