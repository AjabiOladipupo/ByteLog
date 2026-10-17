"""Data plans: CRUD, threshold parsing and the plan card's figures.

A plan counts rx+tx over ``daily`` rows in its current cycle ``[start, end)`` for the
networks in ``plan_networks`` (no rows = all networks). Thresholds are integer percents
1..1000; a threshold is crossed when ``used * 100 >= t * cap`` (integer maths).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, tzinfo
from typing import Final

from bytelog import timeutil
from bytelog.storage import billing
from bytelog.storage.db import write_tx
from bytelog.storage.models import Cycle, Plan, PlanStatus

MIN_THRESHOLD = 1
MAX_THRESHOLD = 1000
DEFAULT_THRESHOLDS = (80, 100)


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()


# --- thresholds -------------------------------------------------------------------------


def parse_thresholds(text: str) -> list[int]:
    """``"100, 80%;50"`` -> ``[50, 80, 100]``. Empty text means no alerts.

    Raises ValueError for anything that is not a whole number between 1 and 1000.
    """
    values: list[int] = []
    for token in re.split(r"[,;\s]+", text.strip()):
        if not token:
            continue
        cleaned = token.removesuffix("%")
        if not re.fullmatch(r"\d+", cleaned):
            raise ValueError(f"{token!r} is not a whole-number percentage")
        values.append(int(cleaned))
    return normalize_thresholds(values)


def normalize_thresholds(values: Iterable[int] | str) -> list[int]:
    """Validate, sort and de-duplicate thresholds (accepts text too)."""
    if isinstance(values, str):
        return parse_thresholds(values)
    result = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"threshold {value!r} is not a whole number")
        if not MIN_THRESHOLD <= value <= MAX_THRESHOLD:
            raise ValueError(f"threshold {value}% is outside {MIN_THRESHOLD}–{MAX_THRESHOLD}%")
        result.add(value)
    return sorted(result)


def format_thresholds(values: Iterable[int]) -> str:
    return ",".join(str(v) for v in values)


def crossed_thresholds(used: int, cap: int, thresholds: Iterable[int]) -> list[int]:
    """Thresholds reached by ``used`` against ``cap``, ascending."""
    if cap <= 0:
        return []
    return sorted(t for t in thresholds if used * 100 >= t * cap)


# --- reading ----------------------------------------------------------------------------


def _plan_network_ids(conn: sqlite3.Connection, plan_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT network_id FROM plan_networks WHERE plan_id=? ORDER BY network_id", (plan_id,)
    )
    return [int(r[0]) for r in rows]


def _plan_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Plan:
    try:
        thresholds = parse_thresholds(row["alert_thresholds"])
    except ValueError:  # hand-edited database; never crash the card or the collector
        thresholds = list(DEFAULT_THRESHOLDS)
    return Plan(
        id=row["id"],
        name=row["name"],
        cap_bytes=int(row["cap_bytes"]),
        cycle_type=row["cycle_type"],
        cycle_value=int(row["cycle_value"]),
        anchor_date=date.fromisoformat(row["anchor_date"]) if row["anchor_date"] else None,
        thresholds=thresholds,
        active=bool(row["active"]),
        network_ids=_plan_network_ids(conn, row["id"]),
    )


def list_plans(conn: sqlite3.Connection, active_only: bool = False) -> list[Plan]:
    sql = "SELECT * FROM plans" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return [_plan_from_row(conn, row) for row in conn.execute(sql).fetchall()]


def get_plan(conn: sqlite3.Connection, plan_id: int) -> Plan | None:
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    return _plan_from_row(conn, row) if row else None


# --- writing ----------------------------------------------------------------------------


def _validate(
    name: str, cap_bytes: int, cycle_type: str, cycle_value: int, anchor: date | None
) -> str:
    clean = name.strip()
    if not clean:
        raise ValueError("plan name is required")
    if cap_bytes <= 0:
        raise ValueError("the cap must be greater than zero")
    billing.validate_cycle(cycle_type, cycle_value, anchor)
    return clean


def _set_networks_in_tx(conn: sqlite3.Connection, plan_id: int, network_ids: Iterable[int]) -> None:
    conn.execute("DELETE FROM plan_networks WHERE plan_id=?", (plan_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO plan_networks(plan_id, network_id) VALUES(?, ?)",
        [(plan_id, int(n)) for n in network_ids],
    )


def insert_plan_in_tx(
    conn: sqlite3.Connection,
    *,
    name: str,
    cap_bytes: int,
    cycle_type: str,
    cycle_value: int,
    anchor_date: date | None = None,
    thresholds: Iterable[int] | str = DEFAULT_THRESHOLDS,
    network_ids: Iterable[int] = (),
    active: bool = True,
    now_utc: int | None = None,
) -> int:
    """:func:`create_plan` inside a transaction the caller already holds."""
    clean = _validate(name, cap_bytes, cycle_type, cycle_value, anchor_date)
    values = normalize_thresholds(thresholds)
    now = timeutil.now_utc_s() if now_utc is None else now_utc
    cur = conn.execute(
        "INSERT INTO plans(name, cap_bytes, cycle_type, cycle_value, anchor_date,"
        " alert_thresholds, active, created_utc, updated_utc)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            clean,
            int(cap_bytes),
            cycle_type,
            int(cycle_value),
            anchor_date.isoformat() if anchor_date else None,
            format_thresholds(values),
            int(active),
            now,
            now,
        ),
    )
    plan_id = int(cur.lastrowid or 0)
    _set_networks_in_tx(conn, plan_id, network_ids)
    return plan_id


def create_plan(
    conn: sqlite3.Connection,
    *,
    name: str,
    cap_bytes: int,
    cycle_type: str,
    cycle_value: int,
    anchor_date: date | None = None,
    thresholds: Iterable[int] | str = DEFAULT_THRESHOLDS,
    network_ids: Iterable[int] = (),
    active: bool = True,
) -> int:
    """Validate and insert a plan; returns its id. Raises ValueError on bad input."""
    with write_tx(conn):
        return insert_plan_in_tx(
            conn,
            name=name,
            cap_bytes=cap_bytes,
            cycle_type=cycle_type,
            cycle_value=cycle_value,
            anchor_date=anchor_date,
            thresholds=thresholds,
            network_ids=network_ids,
            active=active,
        )


def update_plan(
    conn: sqlite3.Connection,
    plan_id: int,
    *,
    name: str | _Unset = UNSET,
    cap_bytes: int | _Unset = UNSET,
    cycle_type: str | _Unset = UNSET,
    cycle_value: int | _Unset = UNSET,
    anchor_date: date | None | _Unset = UNSET,
    thresholds: Iterable[int] | str | _Unset = UNSET,
    network_ids: Iterable[int] | _Unset = UNSET,
    active: bool | _Unset = UNSET,
    today: date | None = None,
    tz: tzinfo | None = None,
) -> None:
    """Change the given fields (omitted ones keep their value).

    Afterwards, ``alerts_sent`` rows of the current cycle whose threshold is no longer
    crossed (cap raised, scope narrowed, ...) are deleted so they can fire again later.
    Raises KeyError for an unknown plan and ValueError on bad input.
    """
    with write_tx(conn):
        old = get_plan(conn, plan_id)
        if old is None:
            raise KeyError(plan_id)
        new_name = old.name if isinstance(name, _Unset) else name
        new_cap = old.cap_bytes if isinstance(cap_bytes, _Unset) else int(cap_bytes)
        new_type = old.cycle_type if isinstance(cycle_type, _Unset) else cycle_type
        new_value = old.cycle_value if isinstance(cycle_value, _Unset) else int(cycle_value)
        new_anchor = old.anchor_date if isinstance(anchor_date, _Unset) else anchor_date
        new_thresholds = (
            old.thresholds if isinstance(thresholds, _Unset) else normalize_thresholds(thresholds)
        )
        new_active = old.active if isinstance(active, _Unset) else bool(active)
        clean = _validate(new_name, new_cap, new_type, new_value, new_anchor)
        conn.execute(
            "UPDATE plans SET name=?, cap_bytes=?, cycle_type=?, cycle_value=?, anchor_date=?,"
            " alert_thresholds=?, active=?, updated_utc=? WHERE id=?",
            (
                clean,
                new_cap,
                new_type,
                new_value,
                new_anchor.isoformat() if new_anchor else None,
                format_thresholds(new_thresholds),
                int(new_active),
                timeutil.now_utc_s(),
                plan_id,
            ),
        )
        if not isinstance(network_ids, _Unset):
            _set_networks_in_tx(conn, plan_id, network_ids)

        plan = get_plan(conn, plan_id)
        assert plan is not None
        status = plan_status(conn, plan, today=today, tz=tz)
        if status.cycle is not None:
            rows = conn.execute(
                "SELECT threshold FROM alerts_sent WHERE plan_id=? AND cycle_start=?",
                (plan_id, status.cycle.start.isoformat()),
            ).fetchall()
            stale = [int(r[0]) for r in rows if status.used * 100 < int(r[0]) * plan.cap_bytes]
            conn.executemany(
                "DELETE FROM alerts_sent WHERE plan_id=? AND cycle_start=? AND threshold=?",
                [(plan_id, status.cycle.start.isoformat(), t) for t in stale],
            )


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> None:
    """Delete a plan; its plan_networks and alerts_sent rows cascade."""
    with write_tx(conn):
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))


# --- status -----------------------------------------------------------------------------


def plan_usage(conn: sqlite3.Connection, plan: Plan, cycle: Cycle) -> int:
    """rx+tx over ``daily`` in ``[cycle.start, cycle.end)`` for the plan's networks."""
    sql = (
        "SELECT COALESCE(SUM(rx_bytes + tx_bytes), 0) FROM daily"
        " WHERE day_local >= ? AND day_local < ?"
    )
    params: list[object] = [cycle.start.isoformat(), cycle.end.isoformat()]
    if plan.network_ids:
        sql += f" AND network_id IN ({','.join('?' * len(plan.network_ids))})"
        params.extend(plan.network_ids)
    return int(conn.execute(sql, params).fetchone()[0])


def plan_status(
    conn: sqlite3.Connection, plan: Plan, today: date | None = None, tz: tzinfo | None = None
) -> PlanStatus:
    """Figures for the plan card. A rolling plan that has not started has ``cycle=None``,
    zero usage and ``days_left=0``."""
    if today is None:
        today = datetime.now(tz or timeutil.local_zone()).date()
    cycle = billing.cycle_for(plan.cycle_type, plan.cycle_value, plan.anchor_date, today)
    if cycle is None:
        return PlanStatus(
            plan=plan,
            cycle=None,
            used=0,
            remaining=plan.cap_bytes,
            percent=0.0,
            days_left=0,
            daily_allowance=0,
            crossed=[],
            over_by=0,
        )
    used = plan_usage(conn, plan, cycle)
    remaining = max(plan.cap_bytes - used, 0)
    left = billing.days_left(cycle, today)
    return PlanStatus(
        plan=plan,
        cycle=cycle,
        used=used,
        remaining=remaining,
        percent=used * 100.0 / plan.cap_bytes,
        days_left=left,
        daily_allowance=billing.daily_allowance(remaining, left),
        crossed=crossed_thresholds(used, plan.cap_bytes, plan.thresholds),
        over_by=max(used - plan.cap_bytes, 0),
    )
