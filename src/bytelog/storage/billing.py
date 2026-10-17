"""Billing-cycle maths. Pure functions over local calendar dates; no database access.

Cycles are half-open ``[start, end)``. "Monthly on day N" always clamps from N (never
from the previous clamped date), so N=31 gives Jan 31 – Feb 27, Feb 28 – Mar 30,
Mar 31 – Apr 29, ... and never drifts. "Rolling N days" repeats every N days from an
anchor date; before the anchor there is no active cycle.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from bytelog.storage.models import Cycle

MONTHLY_ON_DAY = "monthly_on_day"
ROLLING_DAYS = "rolling_days"
CYCLE_TYPES = (MONTHLY_ON_DAY, ROLLING_DAYS)


def _start_in(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def validate_cycle(cycle_type: str, cycle_value: int, anchor: date | None) -> None:
    """Raise ValueError if the combination can never produce a cycle."""
    if cycle_type == MONTHLY_ON_DAY:
        if not 1 <= cycle_value <= 31:
            raise ValueError("billing day must be between 1 and 31")
    elif cycle_type == ROLLING_DAYS:
        if not 1 <= cycle_value <= 366:
            raise ValueError("cycle length must be between 1 and 366 days")
        if anchor is None:
            raise ValueError("a rolling cycle needs a start date")
    else:
        raise ValueError(f"unknown cycle type {cycle_type!r}")


def cycle_for(cycle_type: str, cycle_value: int, anchor: date | None, today: date) -> Cycle | None:
    """The cycle containing ``today``; None for a rolling plan whose anchor is in the future."""
    validate_cycle(cycle_type, cycle_value, anchor)
    if cycle_type == MONTHLY_ON_DAY:
        this_month = _start_in(today.year, today.month, cycle_value)
        if today >= this_month:
            start = this_month
        else:
            start = _start_in(*_shift_month(today.year, today.month, -1), cycle_value)
        end = _start_in(*_shift_month(start.year, start.month, 1), cycle_value)
        return Cycle(start=start, end=end)
    assert anchor is not None  # validate_cycle guarantees it
    if today < anchor:
        return None
    k = (today - anchor).days // cycle_value
    start = anchor + timedelta(days=k * cycle_value)
    return Cycle(start=start, end=start + timedelta(days=cycle_value))


def days_left(cycle: Cycle, today: date) -> int:
    """Days remaining including today; >= 1 for any ``today`` inside the cycle."""
    return max(0, (cycle.end - today).days)


def daily_allowance(remaining: int, days_left: int) -> int:
    """Bytes per day that keep usage within the cap; 0 when nothing is left."""
    if days_left <= 0 or remaining <= 0:
        return 0
    return remaining // days_left
