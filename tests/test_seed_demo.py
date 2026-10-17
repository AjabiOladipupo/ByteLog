from __future__ import annotations

import importlib.util
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from bytelog.storage import plans
from bytelog.storage.db import open_db
from bytelog.storage.rollup import read_watermark, run_rollup
from bytelog.units import GB

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
LAGOS = ZoneInfo("Africa/Lagos")
NOW = int(datetime(2026, 10, 17, 14, 7, tzinfo=LAGOS).timestamp())


@pytest.fixture(scope="module")
def seed_demo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_demo", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_demo"] = module
    spec.loader.exec_module(module)
    return module


def totals(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT SUM(rx_bytes + tx_bytes) FROM {table}").fetchone()[0]


def test_seed_is_plausible_and_consistent(seed_demo: ModuleType, db: sqlite3.Connection) -> None:
    result = seed_demo.seed(db, days=365, rng=random.Random(7), tz=LAGOS, now_utc=NOW)

    assert result.days == 365
    assert result.networks == 3
    per_day = result.total_bytes / result.days
    assert 0.8 * GB < per_day < 4 * GB

    # Buckets and daily agree; nothing lies in the future.
    assert totals(db, "usage_15m") == totals(db, "daily") == result.total_bytes
    assert db.execute("SELECT MAX(bucket_utc) FROM usage_15m").fetchone()[0] + 900 <= NOW
    assert db.execute("SELECT MAX(day_local) FROM daily").fetchone()[0] == "2026-10-17"
    assert db.execute("SELECT MIN(day_local) FROM daily").fetchone()[0] >= "2025-10-18"

    # Samples only for the last week, already rolled up: a rollup changes nothing.
    assert db.execute("SELECT MIN(ts_utc) FROM samples").fetchone()[0] >= NOW - 7 * 86_400
    assert read_watermark(db) == db.execute("SELECT MAX(id) FROM samples").fetchone()[0]
    before = totals(db, "daily")
    assert run_rollup(db) == 0
    assert totals(db, "daily") == before
    sample_sum = totals(db, "samples")
    week_fine = db.execute(
        "SELECT SUM(rx_bytes + tx_bytes) FROM usage_15m WHERE bucket_utc >= ?",
        (NOW - 7 * 86_400,),
    ).fetchone()[0]
    assert sample_sum == week_fine

    # Download dominates.
    rx, tx = db.execute("SELECT SUM(rx_bytes), SUM(tx_bytes) FROM daily").fetchone()
    assert 0.84 < rx / (rx + tx) < 0.91

    # The demo plan sits at roughly 70-85% of its cap.
    (plan,) = plans.list_plans(db)
    status = plans.plan_status(db, plan, today=datetime.fromtimestamp(NOW, LAGOS).date())
    assert plan.name == "MiFi plan"
    assert 70 <= status.percent <= 85
    assert status.used == result.plan_used
    metered = db.execute("SELECT ssid FROM networks WHERE is_metered=1").fetchall()
    assert [r[0] for r in metered] == ["MTN MiFi"]
    assert db.execute("SELECT state FROM collector_status").fetchone()[0] == "stopped"


def test_seed_refuses_existing_data_unless_reset(
    seed_demo: ModuleType, db: sqlite3.Connection
) -> None:
    seed_demo.seed(db, days=10, rng=random.Random(1), tz=LAGOS, now_utc=NOW)
    with pytest.raises(RuntimeError):
        seed_demo.seed(db, days=10, rng=random.Random(1), tz=LAGOS, now_utc=NOW)
    result = seed_demo.seed(db, days=20, rng=random.Random(2), tz=LAGOS, now_utc=NOW, reset=True)
    assert result.days == 20
    assert len(plans.list_plans(db)) == 1
    assert db.execute("SELECT COUNT(*) FROM networks").fetchone()[0] == 3


def test_main_writes_to_data_dir(seed_demo: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "demo"
    assert seed_demo.main(["--data-dir", str(target), "--days", "30", "--seed", "3"]) == 0
    conn = open_db(target / "bytelog.db")
    try:
        assert conn.execute("SELECT COUNT(DISTINCT day_local) FROM daily").fetchone()[0] >= 29
    finally:
        conn.close()
    assert seed_demo.main(["--data-dir", str(target), "--days", "5"]) == 1


def test_main_refuses_real_database(
    seed_demo: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real = tmp_path / "real" / "bytelog.db"
    monkeypatch.setattr(seed_demo, "real_default_db", lambda: fake_real)
    assert seed_demo.main(["--db", str(fake_real), "--days", "2"]) == 2
    assert not fake_real.exists()
    fake_real.parent.mkdir()
    assert seed_demo.main(["--db", str(fake_real), "--days", "2", "--force"]) == 0
