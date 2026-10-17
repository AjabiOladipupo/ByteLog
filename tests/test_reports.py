from __future__ import annotations

import csv
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from bytelog.reports import export_csv, export_pdf
from bytelog.reports.csv_export import COLUMNS
from bytelog.reports.fonts import register_fonts
from bytelog.reports.pdf_report import MAX_DAILY_BARS, _chart_buckets
from bytelog.storage.db import write_tx
from bytelog.storage.periods import daily_totals
from bytelog.units import GB, MB, format_bytes

START = date(2026, 10, 1)


def add_network(conn: sqlite3.Connection, kind: str, identity: str, **cols: object) -> int:
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO networks(kind, identity, ssid, adapter_name, user_label, is_metered,"
            " first_seen_utc, last_seen_utc) VALUES(?,?,?,?,?,?,0,0)",
            (
                kind,
                identity,
                cols.get("ssid"),
                cols.get("adapter_name"),
                cols.get("user_label"),
                int(bool(cols.get("is_metered", False))),
            ),
        )
    return int(cur.lastrowid or 0)


def add_daily(conn: sqlite3.Connection, day: date, network_id: int, rx: int, tx: int) -> None:
    with write_tx(conn):
        conn.execute(
            "INSERT INTO daily(day_local, network_id, rx_bytes, tx_bytes) VALUES(?,?,?,?)",
            (day.isoformat(), network_id, rx, tx),
        )


@pytest.fixture
def seeded(db: sqlite3.Connection) -> tuple[sqlite3.Connection, int, int]:
    wifi = add_network(db, "wifi", "ssid:4d6946", ssid="MiFi & <Home>", is_metered=True)
    lan = add_network(db, "ethernet", "{GUID-1}", adapter_name="Ethernet")
    for i in range(5):
        day = START + timedelta(days=i)
        add_daily(db, day, wifi, (i + 1) * 300 * MB, (i + 1) * 20 * MB)
        if i % 2 == 0:
            add_daily(db, day, lan, 2 * GB, 100 * MB)
    return db, wifi, lan


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_csv_columns_rows_and_totals(seeded: tuple[sqlite3.Connection, int, int], tmp_path):
    conn, wifi, _lan = seeded
    out = tmp_path / "usage.csv"
    written = export_csv(conn, out, START, START + timedelta(days=4))

    assert written == 8  # 5 wifi days + 3 ethernet days
    with out.open(encoding="utf-8-sig", newline="") as fh:
        assert tuple(next(csv.reader(fh))) == COLUMNS
    rows = read_csv(out)
    assert len(rows) == written
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    assert rows[0]["network"] in {"MiFi & <Home>", "Ethernet"}
    for r in rows:
        rx, tx, total = int(r["download_bytes"]), int(r["upload_bytes"]), int(r["total_bytes"])
        assert rx + tx == total
        assert r["total"] == format_bytes(total)
        assert r["download"] == format_bytes(rx)
    total_bytes = sum(int(r["total_bytes"]) for r in rows)
    expected = sum((i + 1) * 320 * MB for i in range(5)) + 3 * (2 * GB + 100 * MB)
    assert total_bytes == expected
    assert not out.with_name(out.name + ".part").exists()

    only_wifi = tmp_path / "wifi.csv"
    assert export_csv(conn, only_wifi, START, START + timedelta(days=4), [wifi]) == 5
    assert {r["network"] for r in read_csv(only_wifi)} == {"MiFi & <Home>"}


def test_csv_empty_range_writes_header_only(seeded, tmp_path):
    conn, _, _ = seeded
    out = tmp_path / "empty.csv"
    assert export_csv(conn, out, date(2020, 1, 1), date(2020, 1, 31)) == 0
    assert out.read_text(encoding="utf-8-sig").strip() == ",".join(COLUMNS)


def assert_pdf(path: Path) -> bytes:
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(data) > 2000
    assert data.rstrip().endswith(b"%%EOF")
    return data


def test_pdf_normal_range(seeded, tmp_path):
    conn, wifi, _ = seeded
    out = tmp_path / "report.pdf"
    export_pdf(conn, out, START, START + timedelta(days=13))
    assert_pdf(out)
    filtered = tmp_path / "wifi.pdf"
    export_pdf(conn, filtered, START, START + timedelta(days=4), [wifi])
    assert_pdf(filtered)


def test_pdf_empty_range(db, tmp_path):
    out = tmp_path / "empty.pdf"
    export_pdf(db, out, date(2026, 1, 1), date(2026, 1, 1))
    assert_pdf(out)
    export_pdf(db, out, date(2026, 1, 1), date(2026, 1, 31), [999])
    assert_pdf(out)


def test_pdf_long_range_is_charted_per_month(seeded, tmp_path):
    conn, wifi, _ = seeded
    start = START - timedelta(days=399)
    add_daily(conn, start, wifi, 5 * GB, 1 * GB)
    out = tmp_path / "long.pdf"
    export_pdf(conn, out, start, START + timedelta(days=4))
    assert_pdf(out)

    buckets, monthly = _chart_buckets(daily_totals(conn, start, START + timedelta(days=4)))
    assert monthly
    assert len(buckets) == 15  # Aug 2025 .. Oct 2026
    assert buckets[0].label == "Aug 2025"
    assert sum(b.total for b in buckets) == sum(
        b.total for b in daily_totals(conn, start, START + timedelta(days=4))
    )
    days, monthly = _chart_buckets(daily_totals(conn, START, START + timedelta(MAX_DAILY_BARS - 1)))
    assert not monthly and len(days) == MAX_DAILY_BARS


def test_pdf_rejects_reversed_range(db, tmp_path):
    with pytest.raises(ValueError):
        export_pdf(db, tmp_path / "x.pdf", date(2026, 2, 1), date(2026, 1, 1))


def test_plex_fonts_registered():
    assert register_fonts() == ("IBMPlexSans", "IBMPlexMono")


def test_reports_do_not_import_qt():
    code = (
        "import sys, bytelog.reports;"
        "bad = [m for m in sys.modules if m.startswith(('PySide6', 'shiboken6', 'pyqtgraph'))];"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
