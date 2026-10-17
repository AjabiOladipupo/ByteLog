"""Destructive maintenance: "Reset all data" and database backups."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from bytelog.storage.db import write_tx

log = logging.getLogger(__name__)

# Order matters only for readability; foreign keys cascade either way.
_USAGE_TABLES = ("alerts_sent", "daily", "usage_15m", "samples")
_FORGET_TABLES = ("plans", "collector_state", "networks", "adapters")


def reset_usage(conn: sqlite3.Connection, forget_networks_and_plans: bool = False) -> None:
    """Delete all usage in one transaction (joins the caller's transaction if active).

    Keeps ``rollup_state``: ``samples`` uses AUTOINCREMENT, so new ids stay above the
    watermark and are rolled up normally. Settings are always kept. With
    ``forget_networks_and_plans`` also deletes plans (and their network scopes),
    networks, adapters and the collector's baselines; a running collector keeps its
    in-memory baselines and recreates the rows on its next tick.
    """
    tables = _USAGE_TABLES + (_FORGET_TABLES if forget_networks_and_plans else ())

    def _delete() -> None:
        for table in tables:
            conn.execute(f"DELETE FROM {table}")

    if conn.in_transaction:
        _delete()
    else:
        with write_tx(conn):
            _delete()
    log.warning("usage data reset (forget networks and plans: %s)", forget_networks_and_plans)


def backup_database(conn: sqlite3.Connection, dest: Path) -> None:
    """Consistent online copy via the sqlite3 backup API (safe while the collector writes)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    log.info("database backed up to %s", dest)
