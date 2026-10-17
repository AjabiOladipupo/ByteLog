from __future__ import annotations

from datetime import date

import pytest

from bytelog.storage.billing import cycle_for, daily_allowance, days_left
from bytelog.storage.models import Cycle


def monthly(n: int, today: date) -> Cycle | None:
    return cycle_for("monthly_on_day", n, None, today)


@pytest.mark.parametrize(
    ("n", "today", "start", "end"),
    [
        (5, date(2026, 10, 17), date(2026, 10, 5), date(2026, 11, 5)),
        (5, date(2026, 10, 4), date(2026, 9, 5), date(2026, 10, 5)),
        (1, date(2026, 3, 1), date(2026, 3, 1), date(2026, 4, 1)),
        # N=31 clamps from N every month, never from the previous clamped day.
        (31, date(2026, 1, 31), date(2026, 1, 31), date(2026, 2, 28)),
        (31, date(2026, 2, 27), date(2026, 1, 31), date(2026, 2, 28)),
        (31, date(2026, 2, 28), date(2026, 2, 28), date(2026, 3, 31)),
        (31, date(2026, 3, 30), date(2026, 2, 28), date(2026, 3, 31)),
        (31, date(2026, 3, 31), date(2026, 3, 31), date(2026, 4, 30)),
        (31, date(2026, 4, 30), date(2026, 4, 30), date(2026, 5, 31)),
        (30, date(2026, 2, 15), date(2026, 1, 30), date(2026, 2, 28)),
        (30, date(2026, 3, 1), date(2026, 2, 28), date(2026, 3, 30)),
        (29, date(2026, 2, 28), date(2026, 2, 28), date(2026, 3, 29)),
        (29, date(2026, 2, 27), date(2026, 1, 29), date(2026, 2, 28)),
        # Leap year 2028: Feb has a 29th.
        (29, date(2028, 2, 29), date(2028, 2, 29), date(2028, 3, 29)),
        (31, date(2028, 2, 28), date(2028, 1, 31), date(2028, 2, 29)),
        (31, date(2028, 2, 29), date(2028, 2, 29), date(2028, 3, 31)),
        (30, date(2028, 3, 1), date(2028, 2, 29), date(2028, 3, 30)),
        # Year rollover.
        (15, date(2026, 12, 20), date(2026, 12, 15), date(2027, 1, 15)),
        (15, date(2027, 1, 3), date(2026, 12, 15), date(2027, 1, 15)),
        (31, date(2027, 1, 1), date(2026, 12, 31), date(2027, 1, 31)),
    ],
)
def test_monthly_on_day(n: int, today: date, start: date, end: date) -> None:
    assert monthly(n, today) == Cycle(start, end)


def test_today_is_start_and_last_day() -> None:
    cycle = monthly(5, date(2026, 10, 5))
    assert cycle == Cycle(date(2026, 10, 5), date(2026, 11, 5))
    assert days_left(cycle, date(2026, 10, 5)) == 31
    last = monthly(5, date(2026, 11, 4))
    assert last == cycle
    assert days_left(cycle, cycle.last_day) == 1


def test_rolling_days() -> None:
    anchor = date(2026, 10, 1)
    assert cycle_for("rolling_days", 30, anchor, anchor) == Cycle(anchor, date(2026, 10, 31))
    assert cycle_for("rolling_days", 30, anchor, date(2026, 10, 30)) == Cycle(
        anchor, date(2026, 10, 31)
    )
    assert cycle_for("rolling_days", 30, anchor, date(2026, 10, 31)) == Cycle(
        date(2026, 10, 31), date(2026, 11, 30)
    )
    assert cycle_for("rolling_days", 7, anchor, date(2027, 1, 2)) == Cycle(
        date(2026, 12, 31), date(2027, 1, 7)
    )
    assert cycle_for("rolling_days", 1, anchor, date(2026, 10, 9)) == Cycle(
        date(2026, 10, 9), date(2026, 10, 10)
    )


def test_rolling_before_anchor_has_no_cycle() -> None:
    assert cycle_for("rolling_days", 30, date(2026, 11, 1), date(2026, 10, 31)) is None


@pytest.mark.parametrize(
    ("cycle_type", "value", "anchor"),
    [
        ("monthly_on_day", 0, None),
        ("monthly_on_day", 32, None),
        ("rolling_days", 0, date(2026, 1, 1)),
        ("rolling_days", 30, None),
        ("weekly", 1, None),
    ],
)
def test_invalid_cycles(cycle_type: str, value: int, anchor: date | None) -> None:
    with pytest.raises(ValueError):
        cycle_for(cycle_type, value, anchor, date(2026, 10, 17))


def test_days_left_and_allowance() -> None:
    cycle = Cycle(date(2026, 10, 5), date(2026, 11, 5))
    assert days_left(cycle, date(2026, 10, 29)) == 7
    assert daily_allowance(700, 7) == 100
    assert daily_allowance(699, 7) == 99
    assert daily_allowance(0, 7) == 0
    assert daily_allowance(-5, 7) == 0
    assert daily_allowance(100, 0) == 0
