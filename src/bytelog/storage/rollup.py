"""Incremental rollup of ``samples`` into ``usage_15m`` and ``daily``.

Samples are selected by id above the ``rollup_state`` watermark (never by time, so rows
written after the clock moved backwards are still picked up and land in their earlier
buckets). The aggregation, the additive upserts and the watermark update all happen in
one ``BEGIN IMMEDIATE`` transaction: a crash rolls everything back and running twice is
a no-op.
"""

from __future__ import annotations

import logging
import sqlite3

from bytelog.storage.db import write_tx

log = logging.getLogger(__name__)

WATERMARK_NAME = "main"
BUCKET_S = 900

_UPSERT_15M = f"""
    INSERT INTO usage_15m(bucket_utc, network_id, rx_bytes, tx_bytes)
    SELECT ts_utc - (ts_utc % {BUCKET_S}), network_id, SUM(rx_bytes), SUM(tx_bytes)
      FROM samples
     WHERE id > ? AND id <= ?
     GROUP BY 1, 2
     ORDER BY 1, 2
    ON CONFLICT(bucket_utc, network_id) DO UPDATE SET
        rx_bytes = rx_bytes + excluded.rx_bytes,
        tx_bytes = tx_bytes + excluded.tx_bytes
"""

_UPSERT_DAILY = """
    INSERT INTO daily(day_local, network_id, rx_bytes, tx_bytes)
    SELECT date(ts_utc + utc_offset_s, 'unixepoch'), network_id, SUM(rx_bytes), SUM(tx_bytes)
      FROM samples
     WHERE id > ? AND id <= ?
     GROUP BY 1, 2
     ORDER BY 1, 2
    ON CONFLICT(day_local, network_id) DO UPDATE SET
        rx_bytes = rx_bytes + excluded.rx_bytes,
        tx_bytes = tx_bytes + excluded.tx_bytes
"""


def read_watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT last_sample_id FROM rollup_state WHERE name=?", (WATERMARK_NAME,)
    ).fetchone()
    return int(row[0]) if row else 0


def set_watermark_in_tx(conn: sqlite3.Connection, last_sample_id: int) -> None:
    """Store the watermark inside a transaction the caller already holds."""
    conn.execute(
        "INSERT INTO rollup_state(name, last_sample_id) VALUES(?, ?) "
        "ON CONFLICT(name) DO UPDATE SET last_sample_id=excluded.last_sample_id",
        (WATERMARK_NAME, last_sample_id),
    )


def run_rollup(conn: sqlite3.Connection) -> int:
    """Roll every sample above the watermark into the buckets. Returns samples consumed."""
    with write_tx(conn):
        watermark = read_watermark(conn)
        count, max_id = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM samples WHERE id > ?", (watermark,)
        ).fetchone()
        if not count:
            return 0
        conn.execute(_UPSERT_15M, (watermark, max_id))
        conn.execute(_UPSERT_DAILY, (watermark, max_id))
        set_watermark_in_tx(conn, int(max_id))
    log.debug("rolled up %d samples (ids %d..%d)", count, watermark + 1, max_id)
    return int(count)
