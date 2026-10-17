"""Fill a ByteLog database with 12 months of realistic fake usage for demos and UI work.

Writes ``daily`` and ``usage_15m`` directly, raw ``samples`` only for the last 7 days
(consistent with the buckets), and moves the rollup watermark to ``MAX(samples.id)`` in
the same transaction so the collector never rolls the demo samples up a second time.
Collector status and collector baselines are left alone.

Pattern (a Lagos-style week): quiet nights, a busy evening peak, weekends heavier,
85-90% download, MiFi during the weekday daytime and on power-cut evenings, home Wi-Fi
otherwise, Ethernet on some office days, and the odd multi-GB update day. The current
cycle of the demo "MiFi plan" (30 GB, monthly on day 5) is scaled to land at ~74-82%.

Run: ``uv run python scripts/seed_demo.py --data-dir .demo`` (then start the dashboard
with ``--data-dir .demo``). Refuses to touch the real default database without
``--force`` and refuses a database that already has data without ``--reset``.
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from pathlib import Path

from bytelog import timeutil
from bytelog.paths import ENV_HOME, db_path
from bytelog.storage import billing, plans
from bytelog.storage.db import open_db, write_tx
from bytelog.storage.rollup import set_watermark_in_tx
from bytelog.units import GB, MB

BUCKET_S = 900
SAMPLES_PER_BUCKET = 5
SAMPLE_SPAN_S = BUCKET_S // SAMPLES_PER_BUCKET
SAMPLE_DAYS = 7

PLAN_NAME = "MiFi plan"
PLAN_CAP = 30 * GB
PLAN_DAY = 5

WIFI_ADAPTER = ("{6B29FC40-CA47-1067-B31D-00DD010662DA}", "Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz")
ETH_ADAPTER = (
    "{9F1C3E27-5A4B-4C2D-8E7F-1A2B3C4D5E6F}",
    "Ethernet",
    "Realtek PCIe GbE Family Controller",
)

MIFI, HOME, ETHERNET = "MTN MiFi", "Home Wi-Fi", "Ethernet"

# Mean MB per 15-minute bucket by local hour: quiet night, busy evening (~1.25 GB/day).
HOURLY_MB = [8, 2, 2, 2, 2, 2, 6, 6, 12, 12, 12, 12, 14, 14, 14, 14, 14, 18, 18, 30, 30, 30, 30, 15]

USAGE_TABLES = ("samples", "usage_15m", "daily", "networks", "plans")


@dataclass(frozen=True)
class SeedResult:
    days: int
    networks: int
    fine_rows: int
    daily_rows: int
    samples: int
    total_bytes: int
    plan_used: int


@dataclass(frozen=True)
class _DayPlan:
    weekend: bool
    factor: float
    office: bool  # Ethernet during working hours
    power_cut_evening: bool  # home Wi-Fi down -> MiFi in the evening
    update_hour: int | None  # a big download (Windows/game update) starts this hour
    update_bytes: int


def real_default_db() -> Path:
    """The user's real database path, computed without creating any directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "ByteLog" / "bytelog.db"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "bytelog" / "bytelog.db"


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())


def _plan_day(rng: random.Random, day: date) -> _DayPlan:
    weekend = day.weekday() >= 5
    big = rng.random() < 0.04
    return _DayPlan(
        weekend=weekend,
        factor=rng.uniform(0.6, 1.6),
        office=not weekend and rng.random() < 0.3,
        power_cut_evening=rng.random() < 0.25,
        update_hour=rng.randint(19, 22) if big else None,
        update_bytes=int(rng.uniform(1.0, 3.0) * GB) if big else 0,
    )


def _network_for(plan: _DayPlan, hour: int) -> str:
    if not plan.weekend and 8 <= hour < 17:
        return ETHERNET if plan.office else MIFI
    if plan.weekend and 12 <= hour < 16:
        return MIFI  # out and about
    if plan.power_cut_evening and 17 <= hour < 23:
        return MIFI
    return HOME


def _generate(
    rng: random.Random, tz: tzinfo, first_day: date, now_utc: int
) -> dict[tuple[int, str], list[int]]:
    """(bucket_utc, network name) -> [rx, tx] for complete buckets up to now."""
    buckets: dict[tuple[int, str], list[int]] = {}
    bucket = timeutil.local_midnight_utc_s(first_day, tz)
    bucket -= bucket % BUCKET_S
    day_plans: dict[date, _DayPlan] = {}
    update_left: dict[date, int] = {}
    while bucket + BUCKET_S <= now_utc:
        local = timeutil.to_local(bucket, tz)
        day = local.date()
        if day not in day_plans:
            day_plans[day] = _plan_day(rng, day)
            update_left[day] = day_plans[day].update_bytes
        plan = day_plans[day]
        network = _network_for(plan, local.hour)
        mean = HOURLY_MB[local.hour] * MB * plan.factor * (1.3 if plan.weekend else 1.0)
        total = 0 if rng.random() < 0.08 else int(mean * rng.lognormvariate(0, 0.5))
        if plan.update_hour is not None and local.hour >= plan.update_hour and update_left[day]:
            chunk = min(update_left[day], int(rng.uniform(0.15, 0.4) * GB))
            update_left[day] -= chunk
            total += chunk
        if total > 0:
            rx = int(total * rng.uniform(0.85, 0.90))
            buckets[(bucket, network)] = [rx, total - rx]
        bucket += BUCKET_S
    return buckets


def _scale_plan_cycle(
    rng: random.Random,
    tz: tzinfo,
    buckets: dict[tuple[int, str], list[int]],
    today: date,
) -> None:
    """Scale MiFi usage of the current cycle so the plan sits at ~74-82% of its cap."""
    cycle = billing.cycle_for(billing.MONTHLY_ON_DAY, PLAN_DAY, None, today)
    assert cycle is not None
    keys = [
        key
        for key in buckets
        if key[1] == MIFI and cycle.start <= timeutil.local_date(key[0], tz) < cycle.end
    ]
    used = sum(sum(buckets[key]) for key in keys)
    if not used:
        return
    factor = PLAN_CAP * rng.uniform(0.74, 0.82) / used
    for key in keys:
        rx, tx = buckets[key]
        buckets[key] = [int(rx * factor), int(tx * factor)]


def _split(value: int, parts: int) -> list[int]:
    base, extra = divmod(value, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def has_data(conn: sqlite3.Connection) -> bool:
    return any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in USAGE_TABLES)


def _clear_in_tx(conn: sqlite3.Connection) -> None:
    for table in ("samples", "usage_15m", "daily", "alerts_sent", "plan_networks", "plans"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM networks")
    conn.execute("DELETE FROM adapters")  # cascades collector_state


def seed(
    conn: sqlite3.Connection,
    *,
    days: int = 365,
    rng: random.Random | None = None,
    tz: tzinfo | None = None,
    now_utc: int | None = None,
    reset: bool = False,
) -> SeedResult:
    """Write the demo data in one transaction. Raises RuntimeError if data exists."""
    rng = rng or random.Random()
    tz = tz or timeutil.local_zone()
    now = timeutil.now_utc_s() if now_utc is None else now_utc
    today = timeutil.local_date(now, tz)
    first_day = today - timedelta(days=max(1, days) - 1)
    first_seen = timeutil.local_midnight_utc_s(first_day, tz)

    buckets = _generate(rng, tz, first_day, now)
    _scale_plan_cycle(rng, tz, buckets, today)

    with write_tx(conn):
        if has_data(conn):
            if not reset:
                raise RuntimeError("the database already has data; use --reset to replace it")
            _clear_in_tx(conn)

        adapter_ids: dict[str, int] = {}
        for guid, name, description, kind in (
            (*WIFI_ADAPTER, "wifi"),
            (*ETH_ADAPTER, "ethernet"),
        ):
            cur = conn.execute(
                "INSERT INTO adapters(guid, name, description, kind, first_seen_utc,"
                " last_seen_utc) VALUES(?, ?, ?, ?, ?, ?)",
                (guid, name, description, kind, first_seen, now),
            )
            adapter_ids[kind] = int(cur.lastrowid or 0)

        network_ids: dict[str, int] = {}
        network_adapter: dict[str, int] = {}
        for name, kind, metered in (
            (MIFI, "wifi", 1),
            (HOME, "wifi", 0),
            (ETHERNET, "ethernet", 0),
        ):
            if kind == "wifi":
                identity, ssid, adapter_name = "ssid:" + name.encode().hex(), name, WIFI_ADAPTER[1]
            else:
                identity, ssid, adapter_name = ETH_ADAPTER[0], None, ETH_ADAPTER[1]
            cur = conn.execute(
                "INSERT INTO networks(kind, identity, ssid, adapter_name, is_metered,"
                " first_seen_utc, last_seen_utc) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (kind, identity, ssid, adapter_name, metered, first_seen, now),
            )
            network_ids[name] = int(cur.lastrowid or 0)
            network_adapter[name] = adapter_ids[kind]

        fine_rows = []
        daily: defaultdict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0])
        sample_rows = []
        sample_from = now - SAMPLE_DAYS * 86_400
        for (bucket, name), (rx, tx) in sorted(buckets.items()):
            net = network_ids[name]
            fine_rows.append((bucket, net, rx, tx))
            day_sum = daily[(timeutil.local_date(bucket, tz).isoformat(), net)]
            day_sum[0] += rx
            day_sum[1] += tx
            if bucket < sample_from:
                continue
            for k, (part_rx, part_tx) in enumerate(
                zip(_split(rx, SAMPLES_PER_BUCKET), _split(tx, SAMPLES_PER_BUCKET), strict=True)
            ):
                ts = bucket + SAMPLE_SPAN_S * (k + 1) - 1  # stays inside this bucket
                offset = timeutil.utc_offset_s(ts, tz)
                sample_rows.append(
                    (ts, SAMPLE_SPAN_S, offset, net, network_adapter[name], part_rx, part_tx)
                )

        conn.executemany(
            "INSERT INTO usage_15m(bucket_utc, network_id, rx_bytes, tx_bytes) VALUES(?,?,?,?)",
            fine_rows,
        )
        conn.executemany(
            "INSERT INTO daily(day_local, network_id, rx_bytes, tx_bytes) VALUES(?,?,?,?)",
            [(day, net, rx, tx) for (day, net), (rx, tx) in sorted(daily.items())],
        )
        conn.executemany(
            "INSERT INTO samples(ts_utc, span_s, utc_offset_s, network_id, adapter_id,"
            " rx_bytes, tx_bytes, flags) VALUES(?,?,?,?,?,?,?,0)",
            sample_rows,
        )
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM samples").fetchone()[0]
        set_watermark_in_tx(conn, int(max_id))

        plan_id = plans.insert_plan_in_tx(
            conn,
            name=PLAN_NAME,
            cap_bytes=PLAN_CAP,
            cycle_type=billing.MONTHLY_ON_DAY,
            cycle_value=PLAN_DAY,
            thresholds=(80, 100),
            network_ids=[network_ids[MIFI]],
            now_utc=now,
        )
        plan = plans.get_plan(conn, plan_id)
        assert plan is not None
        plan_used = plans.plan_status(conn, plan, today=today).used

    return SeedResult(
        days=(today - first_day).days + 1,
        networks=len(network_ids),
        fine_rows=len(fine_rows),
        daily_rows=len(daily),
        samples=len(sample_rows),
        total_bytes=sum(rx + tx for rx, tx in daily.values()),
        plan_used=plan_used,
    )


def _target(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    if args.data_dir:
        directory = Path(args.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "bytelog.db"
    # Honour BYTELOG_HOME; otherwise name the real path without creating its folder.
    return db_path() if os.environ.get(ENV_HOME) else real_default_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--db", help="database file to fill")
    where.add_argument("--data-dir", help="ByteLog data directory (uses <dir>/bytelog.db)")
    parser.add_argument("--days", type=int, default=365, help="days of history (default 365)")
    parser.add_argument("--seed", type=int, default=None, help="random seed for repeatable data")
    parser.add_argument("--reset", action="store_true", help="replace existing data")
    parser.add_argument(
        "--force", action="store_true", help="allow writing to the real default database"
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    target = _target(args)
    if _same_file(target, real_default_db()) and not args.force:
        print(
            f"Refusing to write demo data into your real database ({target}).\n"
            "Use --data-dir/--db for a separate database, or --force if you really mean it.",
            file=sys.stderr,
        )
        return 2

    conn = open_db(target)
    try:
        result = seed(conn, days=args.days, rng=random.Random(args.seed), reset=args.reset)
    except RuntimeError as exc:
        print(f"{target}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(
        f"Seeded {target}: {result.days} days, {result.networks} networks, "
        f"{result.daily_rows} daily rows, {result.fine_rows} 15-min rows, "
        f"{result.samples} samples, {result.total_bytes / GB:.1f} GB in total; "
        f"{PLAN_NAME} at {result.plan_used * 100 / PLAN_CAP:.0f}% of its cap."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
