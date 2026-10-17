"""Daily per-network usage as CSV.

One row per (local day, network) that has data, in date order. Byte columns are exact
integers for spreadsheets; the formatted columns match the dashboard. Written as UTF-8
with a BOM so Excel shows non-ASCII network names correctly.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from datetime import date
from pathlib import Path

from bytelog.storage.periods import daily_rows
from bytelog.units import format_bytes

COLUMNS = (
    "date",
    "network",
    "download_bytes",
    "upload_bytes",
    "total_bytes",
    "download",
    "upload",
    "total",
)


def export_csv(
    conn: sqlite3.Connection,
    path: Path,
    start: date,
    end: date,
    network_ids: list[int] | None = None,
) -> int:
    """Write usage for local days ``start``..``end`` (inclusive). Returns data rows written."""
    rows = daily_rows(conn, start, end, network_ids or None)
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            for row in rows:
                writer.writerow(
                    (
                        row.day.isoformat(),
                        row.network_label,
                        row.rx,
                        row.tx,
                        row.total,
                        format_bytes(row.rx),
                        format_bytes(row.tx),
                        format_bytes(row.total),
                    )
                )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return len(rows)
