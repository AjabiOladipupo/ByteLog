from __future__ import annotations

import sqlite3

from bytelog.storage.db import write_tx
from bytelog.storage.rollup import read_watermark, run_rollup

T0 = 1_760_000_400  # 2025-10-09 09:00:00 UTC, a multiple of 900


def make_network(conn: sqlite3.Connection, name: str = "Home") -> tuple[int, int]:
    with write_tx(conn):
        adapter = conn.execute(
            "INSERT INTO adapters(guid, name, kind, first_seen_utc, last_seen_utc)"
            " VALUES(?, 'Wi-Fi', 'wifi', 0, 0)",
            (f"guid-{name}",),
        ).lastrowid
        network = conn.execute(
            "INSERT INTO networks(kind, identity, ssid, first_seen_utc, last_seen_utc)"
            " VALUES('wifi', ?, ?, 0, 0)",
            (f"ssid:{name}", name),
        ).lastrowid
    return int(network or 0), int(adapter or 0)


def add_samples(
    conn: sqlite3.Connection,
    rows: list[tuple[int, int, int]],
    network: int,
    adapter: int,
    offset: int = 3600,
) -> None:
    with write_tx(conn):
        conn.executemany(
            "INSERT INTO samples(ts_utc, span_s, utc_offset_s, network_id, adapter_id,"
            " rx_bytes, tx_bytes) VALUES(?, 10, ?, ?, ?, ?, ?)",
            [(ts, offset, network, adapter, rx, tx) for ts, rx, tx in rows],
        )


def fine(conn: sqlite3.Connection) -> dict[int, tuple[int, int]]:
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT bucket_utc, rx_bytes, tx_bytes FROM usage_15m")
    }


def days(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    return {
        r[0]: (r[1], r[2]) for r in conn.execute("SELECT day_local, rx_bytes, tx_bytes FROM daily")
    }


def test_empty_rollup_is_noop(db: sqlite3.Connection) -> None:
    assert run_rollup(db) == 0
    assert read_watermark(db) == 0


def test_rollup_twice_does_not_double_count(db: sqlite3.Connection) -> None:
    net, ad = make_network(db)
    add_samples(db, [(T0 + 10, 100, 10), (T0 + 20, 200, 20), (T0 + 910, 50, 5)], net, ad)

    assert run_rollup(db) == 3
    first = (fine(db), days(db))
    assert run_rollup(db) == 0
    assert (fine(db), days(db)) == first
    assert fine(db) == {T0: (300, 30), T0 + 900: (50, 5)}
    assert days(db) == {"2025-10-09": (350, 35)}


def test_watermark_with_interleaved_inserts(db: sqlite3.Connection) -> None:
    net, ad = make_network(db)
    add_samples(db, [(T0 + 10, 1, 1), (T0 + 20, 2, 2)], net, ad)
    assert run_rollup(db) == 2
    add_samples(db, [(T0 + 30, 4, 4)], net, ad)
    assert run_rollup(db) == 1
    add_samples(db, [(T0 + 40, 8, 8), (T0 + 50, 16, 16)], net, ad)
    assert run_rollup(db) == 2
    assert run_rollup(db) == 0

    assert fine(db) == {T0: (31, 31)}
    assert read_watermark(db) == db.execute("SELECT MAX(id) FROM samples").fetchone()[0]


def test_clock_back_rows_land_in_earlier_buckets(db: sqlite3.Connection) -> None:
    net, ad = make_network(db)
    add_samples(db, [(T0 + 7200, 100, 0)], net, ad)
    run_rollup(db)
    # The clock jumped back two hours (and a day for daily): written later, earlier ts.
    add_samples(db, [(T0 + 60, 7, 1), (T0 - 86_400, 3, 3)], net, ad)
    assert run_rollup(db) == 2

    assert fine(db) == {T0 + 7200: (100, 0), T0: (7, 1), T0 - 86_400: (3, 3)}
    assert days(db) == {"2025-10-09": (107, 1), "2025-10-08": (3, 3)}


def test_daily_uses_the_offset_stored_with_each_sample(db: sqlite3.Connection) -> None:
    net, ad = make_network(db)
    late = 1_760_052_600  # 2025-10-09 23:30 UTC
    add_samples(db, [(late, 10, 0)], net, ad, offset=3600)  # Lagos: 00:30 on the 10th
    add_samples(db, [(late, 20, 0)], net, ad, offset=-4 * 3600)  # New York: 19:30 on the 9th
    run_rollup(db)
    assert days(db) == {"2025-10-10": (10, 0), "2025-10-09": (20, 0)}


def test_rollup_separates_networks(db: sqlite3.Connection) -> None:
    home, ad = make_network(db, "Home")
    mifi, _ = make_network(db, "MiFi")
    add_samples(db, [(T0 + 5, 1, 0)], home, ad)
    add_samples(db, [(T0 + 6, 2, 0)], mifi, ad)
    run_rollup(db)
    rows = db.execute("SELECT network_id, rx_bytes FROM usage_15m ORDER BY network_id")
    assert [tuple(r) for r in rows] == [(home, 1), (mifi, 2)]
