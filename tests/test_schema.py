from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from bytelog.platform.base import AdapterInfo
from bytelog.storage import maintenance, repo
from bytelog.storage.db import connect, open_db, write_tx
from bytelog.storage.schema import LATEST_VERSION, SchemaTooNewError, current_version, migrate

TABLES = {
    "meta",
    "settings",
    "adapters",
    "networks",
    "samples",
    "usage_15m",
    "daily",
    "rollup_state",
    "plans",
    "plan_networks",
    "alerts_sent",
    "collector_state",
    "collector_status",
}


def tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not r[0].startswith("sqlite_")
    }


def test_migrate_creates_schema(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "a.db")
    assert current_version(conn) == LATEST_VERSION
    assert tables(conn) >= TABLES
    assert [tuple(r) for r in conn.execute("SELECT * FROM rollup_state")] == [("main", 0)]
    assert tuple(conn.execute("SELECT state, beat FROM collector_status").fetchone()) == (
        "stopped",
        0,
    )
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    conn = open_db(path)
    assert migrate(conn) == LATEST_VERSION
    assert migrate(conn) == LATEST_VERSION
    conn.close()
    conn = open_db(path)  # reopen: no error, no duplicated seed rows
    assert conn.execute("SELECT COUNT(*) FROM rollup_state").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM collector_status").fetchone()[0] == 1
    conn.close()


def test_concurrent_migrations_are_safe(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    workers = 6
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    versions: list[int] = []

    def worker() -> None:
        try:
            conn = connect(path)
            barrier.wait()
            versions.append(migrate(conn))
            conn.close()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert versions == [LATEST_VERSION] * workers
    conn = connect(path)
    assert conn.execute("SELECT COUNT(*) FROM rollup_state").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM collector_status").fetchone()[0] == 1
    conn.close()


def test_newer_schema_refused(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "a.db")
    with write_tx(conn):
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (LATEST_VERSION + 1,))
    with pytest.raises(SchemaTooNewError):
        migrate(conn)
    conn.close()


def test_negative_deltas_rejected_by_schema(db: sqlite3.Connection) -> None:
    adapter_id = repo.upsert_adapter(db, WIFI, 1)
    network_id = repo.upsert_network(db, "wifi", "ssid:00", "x", "Wi-Fi", 1)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO samples(ts_utc, span_s, utc_offset_s, network_id, adapter_id, rx_bytes,"
            " tx_bytes) VALUES(1, 10, 0, ?, ?, -5, 0)",
            (network_id, adapter_id),
        )


# --- repo / maintenance (storage helpers owned with the collector) -------------------

WIFI = AdapterInfo("Wi-Fi", "{W}", "Intel(R) Wi-Fi 6 AX201", "wifi", True, True)


def test_upserts_return_stable_ids(db: sqlite3.Connection) -> None:
    a1 = repo.upsert_adapter(db, WIFI, 100)
    a2 = repo.upsert_adapter(db, AdapterInfo("WLAN", "{W}", "Intel", "wifi", True, True), 200)
    assert a1 == a2
    (adapter,) = repo.list_adapters(db)
    assert (adapter.name, adapter.first_seen_utc, adapter.last_seen_utc) == ("WLAN", 100, 200)
    assert adapter.include_override is None

    n1 = repo.upsert_network(db, "ethernet", "{E}", None, "Ethernet", 100)
    n2 = repo.upsert_network(db, "ethernet", "{E}", None, "Ethernet 2", 300)
    n3 = repo.upsert_network(db, "wifi_unknown", "{E}", None, "Ethernet", 300)
    assert n1 == n2 != n3
    net = next(n for n in repo.list_networks(db) if n.id == n1)
    assert (net.adapter_name, net.first_seen_utc, net.last_seen_utc) == ("Ethernet 2", 100, 300)
    with pytest.raises(ValueError):
        repo.upsert_network(db, "bogus", "x", None, None, 1)


def test_update_network_and_override(db: sqlite3.Connection) -> None:
    nid = repo.upsert_network(db, "wifi", "ssid:41", "A", "Wi-Fi", 1)
    repo.update_network(db, nid, user_label="  MiFi  ", is_metered=True)
    net = repo.get_network(db, nid)
    assert net is not None and net.user_label == "MiFi" and net.is_metered and net.label == "MiFi"
    repo.update_network(db, nid, is_metered=False)  # label untouched
    net = repo.get_network(db, nid)
    assert net is not None and net.user_label == "MiFi" and not net.is_metered
    repo.update_network(db, nid, user_label=None)
    assert repo.get_network(db, nid).user_label is None  # type: ignore[union-attr]

    aid = repo.upsert_adapter(db, WIFI, 1)
    repo.set_adapter_override(db, aid, False)
    assert repo.adapter_overrides(db) == {"{W}": False}
    repo.set_adapter_override(db, aid, None)
    assert repo.list_adapters(db)[0].include_override is None


def test_heartbeat(db: sqlite3.Connection) -> None:
    with write_tx(db):
        repo.write_heartbeat(
            db, pid=7, state="running", version="0.1.0", now_utc=50, started_utc=40
        )
        repo.write_heartbeat(
            db, pid=7, state="paused", version="0.1.0", last_error="boom", now_utc=60
        )
    s = repo.read_collector_status(db)
    assert (s.pid, s.state, s.started_utc, s.heartbeat_utc, s.beat) == (7, "paused", 40, 60, 2)
    assert s.last_error == "boom"


def _seed_usage(db: sqlite3.Connection) -> int:
    aid = repo.upsert_adapter(db, WIFI, 1)
    nid = repo.upsert_network(db, "wifi", "ssid:41", "A", "Wi-Fi", 1)
    with write_tx(db):
        for i in range(3):
            db.execute(
                "INSERT INTO samples(ts_utc, span_s, utc_offset_s, network_id, adapter_id,"
                " rx_bytes, tx_bytes) VALUES(?, 10, 3600, ?, ?, 10, 1)",
                (1000 + i, nid, aid),
            )
        db.execute("INSERT INTO daily VALUES('2026-10-17', ?, 30, 3)", (nid,))
        db.execute("INSERT INTO usage_15m VALUES(900, ?, 30, 3)", (nid,))
        db.execute("UPDATE rollup_state SET last_sample_id=3")
        db.execute(
            "INSERT INTO plans(name, cap_bytes, cycle_type, cycle_value, created_utc,"
            " updated_utc) VALUES('MiFi', 100, 'monthly_on_day', 1, 1, 1)"
        )
        db.execute("INSERT INTO plan_networks VALUES(1, ?)", (nid,))
        db.execute("INSERT INTO alerts_sent VALUES(1, '2026-10-01', 80, 1, 90, 0)")
        db.execute(
            "INSERT INTO collector_state VALUES(?, 5, 5, 1, ?, 0)",
            (aid, nid),
        )
    return nid


def count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_reset_usage_keeps_networks_plans_and_watermark(db: sqlite3.Connection) -> None:
    _seed_usage(db)
    maintenance.reset_usage(db)
    for table in ("samples", "daily", "usage_15m", "alerts_sent"):
        assert count(db, table) == 0
    for table in ("networks", "adapters", "plans", "plan_networks", "collector_state"):
        assert count(db, table) == 1
    assert db.execute("SELECT last_sample_id FROM rollup_state").fetchone()[0] == 3
    # AUTOINCREMENT: new ids stay above the watermark, so they are still rolled up.
    nid = repo.list_networks(db)[0].id
    aid = repo.list_adapters(db)[0].id
    db.execute(
        "INSERT INTO samples(ts_utc, span_s, utc_offset_s, network_id, adapter_id, rx_bytes,"
        " tx_bytes) VALUES(2000, 10, 0, ?, ?, 1, 1)",
        (nid, aid),
    )
    assert db.execute("SELECT MAX(id) FROM samples").fetchone()[0] == 4


def test_reset_usage_forget(db: sqlite3.Connection) -> None:
    _seed_usage(db)
    maintenance.reset_usage(db, forget_networks_and_plans=True)
    for table in (
        "samples",
        "daily",
        "usage_15m",
        "alerts_sent",
        "networks",
        "adapters",
        "plans",
        "plan_networks",
        "collector_state",
    ):
        assert count(db, table) == 0, table
    assert count(db, "rollup_state") == 1
    assert count(db, "collector_status") == 1


def test_reset_usage_joins_open_transaction(db: sqlite3.Connection) -> None:
    _seed_usage(db)
    with pytest.raises(RuntimeError), write_tx(db):
        maintenance.reset_usage(db)
        raise RuntimeError("abort")
    assert count(db, "samples") == 3  # rolled back together with the caller


def test_backup_database(db: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_usage(db)
    dest = tmp_path / "backups" / "copy.db"
    maintenance.backup_database(db, dest)
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 3
    copy.close()
