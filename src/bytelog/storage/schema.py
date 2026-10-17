"""Schema and migration runner.

Migrations are ordered Python constants (no data files to bundle). Each migration's
statements run one by one inside a single ``BEGIN IMMEDIATE`` transaction, which also
serialises two processes that start at the same time. ``executescript`` is never used
because it commits implicitly.

All timestamps are INTEGER UTC epoch seconds. All byte counts are INTEGER bytes.
"""

from __future__ import annotations

import logging
import sqlite3

from bytelog.storage.db import write_tx

log = logging.getLogger(__name__)

SCHEMA_V1: list[str] = [
    """CREATE TABLE IF NOT EXISTS meta(
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE settings(
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    # Physical (or user-included) network interfaces, keyed by a stable GUID so that
    # renaming "Ethernet 2" does not lose baselines or create duplicate networks.
    """CREATE TABLE adapters(
        id               INTEGER PRIMARY KEY,
        guid             TEXT NOT NULL UNIQUE,
        name             TEXT NOT NULL,
        description      TEXT NOT NULL DEFAULT '',
        kind             TEXT NOT NULL CHECK(kind IN ('wifi','ethernet','cellular','other')),
        include_override INTEGER CHECK(include_override IN (0,1)),
        first_seen_utc   INTEGER NOT NULL,
        last_seen_utc    INTEGER NOT NULL
    )""",
    # identity: wifi -> 'ssid:<hex of raw SSID bytes>'; ethernet/cellular -> adapter GUID;
    # wifi_unknown (SSID unavailable, e.g. Location permission off) -> adapter GUID.
    """CREATE TABLE networks(
        id             INTEGER PRIMARY KEY,
        kind           TEXT NOT NULL
                       CHECK(kind IN ('wifi','ethernet','cellular','wifi_unknown')),
        identity       TEXT NOT NULL,
        ssid           TEXT,
        adapter_name   TEXT,
        user_label     TEXT,
        is_metered     INTEGER NOT NULL DEFAULT 0 CHECK(is_metered IN (0,1)),
        first_seen_utc INTEGER NOT NULL,
        last_seen_utc  INTEGER NOT NULL,
        UNIQUE(kind, identity)
    )""",
    # Deltas (not cumulative totals). AUTOINCREMENT keeps ids increasing even after the
    # table is emptied, which the rollup watermark relies on. ts_utc is the END of the
    # interval; span_s its length; utc_offset_s the local offset when it was collected.
    # flags: 1=sleep gap, 2=counter reset, 4=clock change, 8=SSID unknown, 16=capped.
    """CREATE TABLE samples(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc       INTEGER NOT NULL,
        span_s       INTEGER NOT NULL CHECK(span_s >= 0),
        utc_offset_s INTEGER NOT NULL,
        network_id   INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
        adapter_id   INTEGER NOT NULL REFERENCES adapters(id) ON DELETE CASCADE,
        rx_bytes     INTEGER NOT NULL CHECK(rx_bytes >= 0),
        tx_bytes     INTEGER NOT NULL CHECK(tx_bytes >= 0),
        flags        INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX samples_ts ON samples(ts_utc)",
    # 15-minute UTC buckets (instead of hourly) so the Today chart is exact in zones with
    # +5:30 / +5:45 offsets. Kept 400 days.
    """CREATE TABLE usage_15m(
        bucket_utc INTEGER NOT NULL,
        network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
        rx_bytes   INTEGER NOT NULL DEFAULT 0,
        tx_bytes   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(bucket_utc, network_id)
    ) WITHOUT ROWID""",
    # day_local = the calendar date where the user was (ts_utc + utc_offset_s). Forever.
    """CREATE TABLE daily(
        day_local  TEXT NOT NULL
                   CHECK(day_local GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
        rx_bytes   INTEGER NOT NULL DEFAULT 0,
        tx_bytes   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(day_local, network_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE rollup_state(
        name           TEXT PRIMARY KEY,
        last_sample_id INTEGER NOT NULL
    ) WITHOUT ROWID""",
    "INSERT INTO rollup_state(name, last_sample_id) VALUES('main', 0)",
    """CREATE TABLE plans(
        id               INTEGER PRIMARY KEY,
        name             TEXT NOT NULL,
        cap_bytes        INTEGER NOT NULL CHECK(cap_bytes > 0),
        cycle_type       TEXT NOT NULL CHECK(cycle_type IN ('monthly_on_day','rolling_days')),
        cycle_value      INTEGER NOT NULL CHECK(
                            (cycle_type = 'monthly_on_day' AND cycle_value BETWEEN 1 AND 31)
                         OR (cycle_type = 'rolling_days' AND cycle_value BETWEEN 1 AND 366)),
        anchor_date      TEXT CHECK(cycle_type = 'monthly_on_day' OR anchor_date IS NOT NULL),
        alert_thresholds TEXT NOT NULL DEFAULT '80,100',
        active           INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_utc      INTEGER NOT NULL,
        updated_utc      INTEGER NOT NULL
    )""",
    # Which networks a plan covers. No rows = all networks.
    """CREATE TABLE plan_networks(
        plan_id    INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
        PRIMARY KEY(plan_id, network_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE alerts_sent(
        plan_id     INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        cycle_start TEXT NOT NULL,
        threshold   INTEGER NOT NULL,
        sent_at_utc INTEGER NOT NULL,
        used_bytes  INTEGER NOT NULL,
        shown       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(plan_id, cycle_start, threshold)
    ) WITHOUT ROWID""",
    """CREATE TABLE collector_state(
        adapter_id      INTEGER PRIMARY KEY REFERENCES adapters(id) ON DELETE CASCADE,
        last_rx         INTEGER NOT NULL,
        last_tx         INTEGER NOT NULL,
        last_ts_utc     INTEGER NOT NULL,
        last_network_id INTEGER REFERENCES networks(id) ON DELETE SET NULL,
        boot_time_utc   INTEGER NOT NULL
    )""",
    """CREATE TABLE collector_status(
        id            INTEGER PRIMARY KEY CHECK(id = 1),
        pid           INTEGER,
        started_utc   INTEGER,
        heartbeat_utc INTEGER,
        beat          INTEGER NOT NULL DEFAULT 0,
        state         TEXT NOT NULL DEFAULT 'stopped'
                      CHECK(state IN ('running','paused','stopped')),
        app_version   TEXT,
        last_error    TEXT
    )""",
    "INSERT INTO collector_status(id) VALUES(1)",
]

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, SCHEMA_V1),
]

LATEST_VERSION = MIGRATIONS[-1][0]


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer ByteLog; never downgrade it."""


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    if current_version(conn) == LATEST_VERSION:
        return LATEST_VERSION
    with write_tx(conn):
        version = current_version(conn)  # re-read under the write lock
        if version > LATEST_VERSION:
            raise SchemaTooNewError(
                f"database schema v{version} is newer than this ByteLog (v{LATEST_VERSION})"
            )
        for target, statements in MIGRATIONS:
            if target <= version:
                continue
            log.info("applying schema migration v%d", target)
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(target),),
            )
            version = target
    return version
