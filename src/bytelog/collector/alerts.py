"""Plan threshold alerts, checked by the collector after each rollup.

De-dup lives in ``alerts_sent`` (primary key plan, cycle_start, threshold): rows are
inserted with INSERT OR IGNORE *before* the toast is shown, so a crash can never alert
twice (at-most-once; a failed toast is logged and left with ``shown=0`` for the
dashboard's tray fallback, not retried). When several thresholds are crossed at once
(catch-up after sleep, a plan created at 95%) all are recorded but only the highest
newly crossed one is shown. A new cycle has a new ``cycle_start`` and so re-arms
automatically; raising a cap re-arms via ``plans.update_plan``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import tzinfo

from bytelog import timeutil
from bytelog.platform.base import Platform
from bytelog.storage import plans
from bytelog.storage.db import write_tx
from bytelog.storage.models import PlanStatus
from bytelog.units import GB, bytes_to_gb, format_bytes

log = logging.getLogger(__name__)

TOAST_TITLE = "ByteLog"


def format_alert(plan_name: str, threshold: int, used: int, cap: int, days_left: int) -> str:
    """``"MiFi plan: 80% used (24.0 of 30.0 GB), 7 days left."``"""
    if cap >= GB:
        amounts = f"{bytes_to_gb(used):.1f} of {bytes_to_gb(cap):.1f} GB"
    else:
        amounts = f"{format_bytes(used)} of {format_bytes(cap)}"
    days = "1 day left" if days_left == 1 else f"{days_left} days left"
    return f"{plan_name}: {threshold}% used ({amounts}), {days}."


@dataclass(frozen=True)
class _Pending:
    status: PlanStatus
    cycle_start: str
    thresholds: list[int]


def _candidates(conn: sqlite3.Connection, today_tz: tzinfo, now_utc: int) -> list[_Pending]:
    """Active plans with crossed thresholds that have no alerts_sent row yet."""
    today = timeutil.local_date(now_utc, today_tz)
    pending: list[_Pending] = []
    for plan in plans.list_plans(conn, active_only=True):
        status = plans.plan_status(conn, plan, today=today)
        if status.cycle is None or not status.crossed:
            continue
        cycle_start = status.cycle.start.isoformat()
        sent = {
            int(r[0])
            for r in conn.execute(
                "SELECT threshold FROM alerts_sent WHERE plan_id=? AND cycle_start=?",
                (plan.id, cycle_start),
            )
        }
        new = [t for t in status.crossed if t not in sent]
        if new:
            pending.append(_Pending(status, cycle_start, new))
    return pending


def check_alerts(
    conn: sqlite3.Connection, platform: Platform, now_utc: int, tz: tzinfo
) -> list[str]:
    """Record newly crossed thresholds and show one toast per plan. Returns messages shown."""
    pending = _candidates(conn, tz, now_utc)
    if not pending:
        return []

    to_show: list[tuple[_Pending, list[int]]] = []
    with write_tx(conn):
        for item in pending:
            inserted = []
            for threshold in item.thresholds:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO alerts_sent(plan_id, cycle_start, threshold,"
                    " sent_at_utc, used_bytes, shown) VALUES(?, ?, ?, ?, ?, 0)",
                    (item.status.plan.id, item.cycle_start, threshold, now_utc, item.status.used),
                )
                if cur.rowcount == 1:
                    inserted.append(threshold)
            if inserted:
                to_show.append((item, inserted))

    shown: list[str] = []
    for item, inserted in to_show:
        status = item.status
        message = format_alert(
            status.plan.name, max(inserted), status.used, status.plan.cap_bytes, status.days_left
        )
        try:
            ok = bool(platform.notify(TOAST_TITLE, message))
        except Exception:
            log.exception("showing the alert toast failed")
            ok = False
        if not ok:
            log.warning("alert not shown as a toast (left for the tray): %s", message)
            continue
        log.info("alert shown: %s", message)
        shown.append(message)
        with write_tx(conn):
            conn.executemany(
                "UPDATE alerts_sent SET shown=1 WHERE plan_id=? AND cycle_start=? AND threshold=?",
                [(status.plan.id, item.cycle_start, t) for t in inserted],
            )
    return shown
