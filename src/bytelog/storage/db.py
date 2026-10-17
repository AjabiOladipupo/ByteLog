"""Connections and transactions.

Both processes share one WAL-mode database. Rules:

* Connections use autocommit (``isolation_level=None``); every write goes through
  :func:`write_tx`, which issues ``BEGIN IMMEDIATE`` so lock upgrades never fail with an
  instant SQLITE_BUSY.
* ``busy_timeout`` lets a writer wait up to 5 s for the other process.
* Reads are short autocommit statements.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bytelog.paths import db_path

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with ByteLog's pragmas. ``":memory:"`` is allowed for tests."""
    target = str(path) if path is not None else str(db_path())
    conn = sqlite3.connect(
        target, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def open_db(path: Path | str | None = None) -> sqlite3.Connection:
    """Connect and bring the schema up to date."""
    from bytelog.storage.schema import migrate

    conn = connect(path)
    migrate(conn)
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection, retries: int = 3) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; rolls back on any exception.

    Only acquiring the write lock is retried; the body runs exactly once.
    """
    for attempt in range(retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if attempt == retries - 1:
                raise
            log.warning("database busy, retrying BEGIN IMMEDIATE (%s)", exc)
            time.sleep(0.2 * (attempt + 1))
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
