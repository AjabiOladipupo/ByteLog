"""Period totals for the Overview page, the tray tooltip and reports.

All date arguments are local calendar dates and every ``start``/``end`` pair here is
INCLUSIVE (unlike billing cycles, which are half-open). Queries read only ``daily`` and
``usage_15m``, never ``samples``.

Today's chart comes from ``usage_15m`` mapped to local two-hour slots; its card totals
come from ``daily`` so they match Week and Month. The two can differ only on a day when
the system timezone changed.

Labels use fixed English names so they do not depend on the process locale.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo

from bytelog import timeutil
from bytelog.storage import settings
from bytelog.storage.models import (
    Bucket,
    DailyRow,
    Network,
    NetworkUsage,
    Period,
    PeriodSummary,
)

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
SLOT_HOURS = 2
SLOTS_PER_DAY = 24 // SLOT_HOURS

BUSIEST_TITLE = "Busiest 2-hour slot"
DAILY_AVERAGE_TITLE = "Daily average"
MONTHLY_AVERAGE_TITLE = "Monthly average"


# --- labels -----------------------------------------------------------------------------


def _dow(day: date) -> str:
    return DAY_NAMES[day.weekday()]


def _mon(day: date) -> str:
    return MONTH_NAMES[day.month - 1]


def day_tooltip(day: date) -> str:
    """``Monday 14 Oct 2026``."""
    return f"{_dow(day)} {day.day} {_mon(day)[:3]} {day.year}"


def month_tooltip(month_start: date) -> str:
    """``January 2026``."""
    return f"{_mon(month_start)} {month_start.year}"


def slot_title(slot: int) -> str:
    """``08:00–10:00`` for slot 4 (the last slot is ``22:00–24:00``)."""
    first = slot * SLOT_HOURS
    return f"{first:02d}:00–{first + SLOT_HOURS:02d}:00"


def date_range_label(start: date, end: date, period: Period | None = None) -> str:
    """Header subtitle for an inclusive range.

    ``Friday 17 October 2026`` for one day, ``Mon 13 Oct – Fri 17 Oct 2026`` within a
    year, ``Mon 29 Dec 2025 – Fri 2 Jan 2026`` across years. With ``period=Period.ALL``
    the label is ``Since March 2026`` (month of ``start``).
    """
    if period is Period.ALL:
        return f"Since {month_tooltip(start)}"
    if start == end:
        return f"{_dow(start)} {start.day} {_mon(start)} {start.year}"

    def short(day: date) -> str:
        return f"{_dow(day)[:3]} {day.day} {_mon(day)[:3]}"

    if start.year == end.year:
        return f"{short(start)} – {short(end)} {end.year}"
    return f"{short(start)} {start.year} – {short(end)} {end.year}"


# --- small helpers ----------------------------------------------------------------------


def _today(tz: tzinfo) -> date:
    return datetime.now(tz).date()


def _network_filter(network_ids: Sequence[int] | None) -> tuple[str, list[int]]:
    """SQL fragment ``AND network_id IN (...)``; None or empty means all networks."""
    if not network_ids:
        return "", []
    marks = ",".join("?" * len(network_ids))
    return f" AND network_id IN ({marks})", [int(n) for n in network_ids]


def _network_from_row(row: sqlite3.Row) -> Network:
    return Network(
        id=row["id"],
        kind=row["kind"],
        identity=row["identity"],
        ssid=row["ssid"],
        adapter_name=row["adapter_name"],
        user_label=row["user_label"],
        is_metered=bool(row["is_metered"]),
        first_seen_utc=row["first_seen_utc"],
        last_seen_utc=row["last_seen_utc"],
    )


def _networks_by_id(conn: sqlite3.Connection) -> dict[int, Network]:
    return {row["id"]: _network_from_row(row) for row in conn.execute("SELECT * FROM networks")}


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _month_starts(first: date, last: date) -> list[date]:
    months: list[date] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --- queries ----------------------------------------------------------------------------


def period_range(period: Period, today: date, week_start: int) -> tuple[date, date]:
    """Inclusive local date range of a period ending today.

    ``week_start`` is 0=Monday ... 6=Sunday. ``Period.ALL`` has no fixed start without the
    database, so it returns the first day of the current month; :func:`summarize` replaces
    that with the first day that has data.
    """
    period = Period(period)
    if period is Period.TODAY:
        return today, today
    if period is Period.WEEK:
        return today - timedelta(days=(today.weekday() - week_start) % 7), today
    return today.replace(day=1), today


def _daily_sums(
    conn: sqlite3.Connection, start: date, end: date, network_ids: Sequence[int] | None
) -> dict[date, tuple[int, int]]:
    where, params = _network_filter(network_ids)
    rows = conn.execute(
        "SELECT day_local, SUM(rx_bytes), SUM(tx_bytes) FROM daily"
        f" WHERE day_local >= ? AND day_local <= ?{where} GROUP BY day_local",
        [start.isoformat(), end.isoformat(), *params],
    )
    return {date.fromisoformat(day): (int(rx), int(tx)) for day, rx, tx in rows}


def daily_totals(
    conn: sqlite3.Connection, start: date, end: date, network_ids: list[int] | None = None
) -> list[Bucket]:
    """One bucket per day in ``[start, end]``, zero-filled. Labels like ``14 Oct``."""
    sums = _daily_sums(conn, start, end, network_ids)
    return [
        Bucket(
            label=f"{day.day} {_mon(day)[:3]}",
            tooltip_title=day_tooltip(day),
            start=day,
            rx=sums.get(day, (0, 0))[0],
            tx=sums.get(day, (0, 0))[1],
        )
        for day in _days(start, end)
    ]


def daily_rows(
    conn: sqlite3.Connection, start: date, end: date, network_ids: list[int] | None = None
) -> list[DailyRow]:
    """Per-day, per-network rows in ``[start, end]`` (only rows that exist), by day."""
    where, params = _network_filter(network_ids)
    networks = _networks_by_id(conn)
    rows = conn.execute(
        "SELECT day_local, network_id, rx_bytes, tx_bytes FROM daily"
        f" WHERE day_local >= ? AND day_local <= ?{where} ORDER BY day_local, network_id",
        [start.isoformat(), end.isoformat(), *params],
    )
    result: list[DailyRow] = []
    for day, network_id, rx, tx in rows:
        network = networks.get(network_id)
        result.append(
            DailyRow(
                day=date.fromisoformat(day),
                network_id=network_id,
                network_label=network.label if network else f"Network {network_id}",
                rx=int(rx),
                tx=int(tx),
            )
        )
    return result


def network_usage(conn: sqlite3.Connection, start: date, end: date) -> list[NetworkUsage]:
    """Usage per network in ``[start, end]`` (networks with traffic only), largest first."""
    rows = conn.execute(
        "SELECT n.*, SUM(d.rx_bytes) AS rx, SUM(d.tx_bytes) AS tx"
        " FROM daily d JOIN networks n ON n.id = d.network_id"
        " WHERE d.day_local >= ? AND d.day_local <= ?"
        " GROUP BY n.id HAVING SUM(d.rx_bytes + d.tx_bytes) > 0",
        (start.isoformat(), end.isoformat()),
    )
    usage = []
    for row in rows:
        network = _network_from_row(row)
        usage.append(
            NetworkUsage(
                network_id=network.id,
                label=network.label,
                kind=network.kind,
                is_metered=network.is_metered,
                rx=int(row["rx"]),
                tx=int(row["tx"]),
            )
        )
    usage.sort(key=lambda u: (-u.total, u.label.casefold()))
    return usage


def today_total(conn: sqlite3.Connection, tz: tzinfo | None = None) -> int:
    """rx+tx for the local today (tray tooltip)."""
    day = _today(tz or timeutil.local_zone())
    row = conn.execute(
        "SELECT COALESCE(SUM(rx_bytes + tx_bytes), 0) FROM daily WHERE day_local = ?",
        (day.isoformat(),),
    ).fetchone()
    return int(row[0])


def first_data_day(conn: sqlite3.Connection, network_ids: list[int] | None = None) -> date | None:
    """Earliest local day with any usage, or None for an empty database."""
    where, params = _network_filter(network_ids)
    row = conn.execute(
        f"SELECT MIN(day_local) FROM daily WHERE (rx_bytes + tx_bytes) > 0{where}", params
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def today_slots(
    conn: sqlite3.Connection, day: date, tz: tzinfo, network_ids: list[int] | None = None
) -> list[Bucket]:
    """Twelve local two-hour buckets of ``day`` built from 15-minute UTC buckets.

    Each 15-minute bucket is mapped to its local wall-clock hour, so this is exact for
    +5:30 / +5:45 offsets and on DST days (where a slot can hold 1 or 3 real hours).
    """
    lo, hi = timeutil.day_bounds_utc_s(day, tz)
    where, params = _network_filter(network_ids)
    rows = conn.execute(
        "SELECT bucket_utc, SUM(rx_bytes), SUM(tx_bytes) FROM usage_15m"
        f" WHERE bucket_utc >= ? AND bucket_utc < ?{where} GROUP BY bucket_utc",
        [lo, hi, *params],
    )
    rx = [0] * SLOTS_PER_DAY
    tx = [0] * SLOTS_PER_DAY
    for bucket_utc, bucket_rx, bucket_tx in rows:
        local = timeutil.to_local(bucket_utc, tz)
        if local.date() != day:  # cannot happen for real offsets; be safe anyway
            continue
        slot = local.hour // SLOT_HOURS
        rx[slot] += int(bucket_rx)
        tx[slot] += int(bucket_tx)
    return [
        Bucket(
            label=f"{slot * SLOT_HOURS:02d}",
            tooltip_title=slot_title(slot),
            start=datetime(day.year, day.month, day.day, slot * SLOT_HOURS, tzinfo=tz),
            rx=rx[slot],
            tx=tx[slot],
        )
        for slot in range(SLOTS_PER_DAY)
    ]


def _summarize_today(
    conn: sqlite3.Connection, today: date, tz: tzinfo, network_ids: list[int] | None
) -> tuple[list[Bucket], int, int, str, int, str]:
    buckets = today_slots(conn, today, tz, network_ids)
    rx, tx = _daily_sums(conn, today, today, network_ids).get(today, (0, 0))
    busiest = max(buckets, key=lambda b: b.total)
    detail = busiest.tooltip_title if busiest.total > 0 else "No usage yet"
    return buckets, rx, tx, BUSIEST_TITLE, busiest.total, detail


def _summarize_days(
    conn: sqlite3.Connection,
    period: Period,
    start: date,
    today: date,
    network_ids: list[int] | None,
) -> tuple[list[Bucket], int, int, str, int, str]:
    sums = _daily_sums(conn, start, today, network_ids)
    buckets = []
    for day in _days(start, today):
        label = f"{_dow(day)[:3]} {day.day}" if period is Period.WEEK else str(day.day)
        day_rx, day_tx = sums.get(day, (0, 0))
        buckets.append(Bucket(label, day_tooltip(day), day, day_rx, day_tx))
    rx = sum(b.rx for b in buckets)
    tx = sum(b.tx for b in buckets)
    n_days = len(buckets)
    return (
        buckets,
        rx,
        tx,
        DAILY_AVERAGE_TITLE,
        (rx + tx) // n_days,
        f"over {_plural(n_days, 'day')}",
    )


def _summarize_months(
    conn: sqlite3.Connection, start: date, today: date, network_ids: list[int] | None
) -> tuple[list[Bucket], int, int, str, int, str]:
    where, params = _network_filter(network_ids)
    rows = conn.execute(
        "SELECT substr(day_local, 1, 7), SUM(rx_bytes), SUM(tx_bytes) FROM daily"
        f" WHERE day_local >= ? AND day_local <= ?{where} GROUP BY 1",
        [start.isoformat(), today.isoformat(), *params],
    )
    sums = {ym: (int(rx), int(tx)) for ym, rx, tx in rows}
    buckets = []
    for month in _month_starts(start, today):
        month_rx, month_tx = sums.get(f"{month:%Y-%m}", (0, 0))
        label = f"{_mon(month)[:3]} {month.year % 100:02d}"
        buckets.append(Bucket(label, month_tooltip(month), month, month_rx, month_tx))
    rx = sum(b.rx for b in buckets)
    tx = sum(b.tx for b in buckets)
    n_months = len(buckets)
    detail = f"over {_plural(n_months, 'month')}"
    return buckets, rx, tx, MONTHLY_AVERAGE_TITLE, (rx + tx) // n_months, detail


def summarize(
    conn: sqlite3.Connection,
    period: Period,
    *,
    today: date | None = None,
    tz: tzinfo | None = None,
    week_start: int | None = None,
    network_ids: list[int] | None = None,
) -> PeriodSummary:
    """Totals, chart buckets, average card and per-network split for one period.

    ``network_ids`` None (or empty) means all networks. For ``Period.ALL`` the summary's
    ``start`` is the first day with data (or the 1st of this month for an empty database)
    and the chart has one bucket per month from that month to the current one.
    """
    period = Period(period)
    tz = tz or timeutil.local_zone()
    today = today or _today(tz)
    if week_start is None:
        week_start = settings.week_start(conn)
    start, end = period_range(period, today, week_start)

    if period is Period.TODAY:
        parts = _summarize_today(conn, today, tz, network_ids)
    elif period is Period.ALL:
        first = first_data_day(conn, network_ids)
        start = min(first, today) if first else start
        parts = _summarize_months(conn, start, today, network_ids)
    else:
        parts = _summarize_days(conn, period, start, today, network_ids)
    buckets, rx, tx, average_title, average_bytes, average_detail = parts

    per_network = network_usage(conn, start, end)
    if network_ids:
        wanted = set(network_ids)
        per_network = [u for u in per_network if u.network_id in wanted]

    return PeriodSummary(
        period=period,
        start=start,
        end=end,
        rx=rx,
        tx=tx,
        buckets=buckets,
        average_title=average_title,
        average_bytes=average_bytes,
        average_detail=average_detail,
        per_network=per_network,
    )
