"""Retention: raw samples for 7 days, 15-minute buckets for 400 days, daily forever.

Samples are deleted only at or below the rollup watermark, so rows that were never rolled
up (collector crashed, rollup failing) survive until they are. Deletes run in small
batches, each in its own short write transaction, so the dashboard never waits long.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from bytelog.storage.db import write_tx
from bytelog.storage.rollup import read_watermark

log = logging.getLogger(__name__)

DAY_S = 86_400
SAMPLE_RETENTION_S = 7 * DAY_S
FINE_RETENTION_S = 400 * DAY_S
BATCH_SIZE = 5000


def _delete_samples_batch(conn: sqlite3.Connection, cutoff: int) -> int:
    with write_tx(conn):
        watermark = read_watermark(conn)
        cur = conn.execute(
            "DELETE FROM samples WHERE id IN ("
            " SELECT id FROM samples WHERE ts_utc < ? AND id <= ? LIMIT ?)",
            (cutoff, watermark, BATCH_SIZE),
        )
        return cur.rowcount


def _delete_fine_batch(conn: sqlite3.Connection, cutoff: int) -> int:
    with write_tx(conn):
        cur = conn.execute(
            "DELETE FROM usage_15m WHERE (bucket_utc, network_id) IN ("
            " SELECT bucket_utc, network_id FROM usage_15m WHERE bucket_utc < ? LIMIT ?)",
            (cutoff, BATCH_SIZE),
        )
        return cur.rowcount


def _drain(
    delete_batch: Callable[[sqlite3.Connection, int], int], conn: sqlite3.Connection, cutoff: int
) -> int:
    total = 0
    while True:
        deleted = delete_batch(conn, cutoff)
        total += deleted
        if deleted < BATCH_SIZE:
            return total


def run_retention(conn: sqlite3.Connection, now_utc: int) -> dict[str, int]:
    """Delete expired rows; returns ``{"samples": n, "usage_15m": m}``."""
    result = {
        "samples": _drain(_delete_samples_batch, conn, now_utc - SAMPLE_RETENTION_S),
        "usage_15m": _drain(_delete_fine_batch, conn, now_utc - FINE_RETENTION_S),
    }
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.OperationalError as exc:  # a checkpoint is an optimisation only
        log.warning("WAL checkpoint failed: %s", exc)
    if result["samples"] or result["usage_15m"]:
        log.info("retention deleted %(samples)d samples, %(usage_15m)d 15-min rows", result)
    return result
