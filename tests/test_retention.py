from __future__ import annotations

import sqlite3

import pytest

from bytelog.storage import retention
from bytelog.storage.db import write_tx
from bytelog.storage.retention import run_retention
from bytelog.storage.rollup import run_rollup

DAY = 86_400
NOW = 1_790_000_100


def setup_network(conn: sqlite3.Connection) -> tuple[int, int]:
    with write_tx(conn):
        adapter = conn.execute(
            "INSERT INTO adapters(guid, name, kind, first_seen_utc, last_seen_utc)"
            " VALUES('g', 'Wi-Fi', 'wifi', 0, 0)"
        ).lastrowid
        network = conn.execute(
            "INSERT INTO networks(kind, identity, first_seen_utc, last_seen_utc)"
            " VALUES('wifi', 'ssid:00', 0, 0)"
        ).lastrowid
    return int(network or 0), int(adapter or 0)


def add_sample(conn: sqlite3.Connection, ts: int, net: int, ad: int) -> None:
    with write_tx(conn):
        conn.execute(
            "INSERT INTO samples(ts_utc, span_s, utc_offset_s, network_id, adapter_id,"
            " rx_bytes, tx_bytes) VALUES(?, 10, 0, ?, ?, 1, 1)",
            (ts, net, ad),
        )


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_deletes_old_rolled_up_samples_only(db: sqlite3.Connection) -> None:
    net, ad = setup_network(db)
    for i in range(3):
        add_sample(db, NOW - 10 * DAY + i, net, ad)  # old
    add_sample(db, NOW - DAY, net, ad)  # recent
    run_rollup(db)
    add_sample(db, NOW - 9 * DAY, net, ad)  # old but not rolled up yet

    result = run_retention(db, NOW)

    assert result["samples"] == 3
    assert count(db, "samples") == 2
    remaining = {r[0] for r in db.execute("SELECT ts_utc FROM samples")}
    assert remaining == {NOW - DAY, NOW - 9 * DAY}
    # After it is rolled up, the straggler goes on the next run.
    run_rollup(db)
    assert run_retention(db, NOW)["samples"] == 1


def test_fine_buckets_kept_400_days_daily_forever(db: sqlite3.Connection) -> None:
    net, _ = setup_network(db)
    old = NOW - 401 * DAY
    old -= old % 900
    with write_tx(db):
        db.executemany(
            "INSERT INTO usage_15m(bucket_utc, network_id, rx_bytes, tx_bytes) VALUES(?, ?, 1, 1)",
            [(old, net), (old + 900, net), (NOW - NOW % 900 - 399 * DAY, net)],
        )
        db.execute("INSERT INTO daily VALUES('2020-01-01', ?, 5, 5)", (net,))

    result = run_retention(db, NOW)

    assert result["usage_15m"] == 2
    assert count(db, "usage_15m") == 1
    assert count(db, "daily") == 1


def test_batches_until_done(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)
    net, ad = setup_network(db)
    for i in range(5):
        add_sample(db, NOW - 30 * DAY + i, net, ad)
    run_rollup(db)
    assert run_retention(db, NOW)["samples"] == 5
    assert count(db, "samples") == 0
