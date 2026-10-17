from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bytelog import timeutil
from bytelog.collector import __main__ as collector_main
from bytelog.collector import service
from bytelog.platform.base import AdapterInfo
from bytelog.platform.fake import FakePlatform
from bytelog.storage import repo, settings
from bytelog.storage.db import open_db

T0 = 1_800_000_000
REAL_LOCAL_ZONE = timeutil.local_zone
ETH = AdapterInfo(
    name="Ethernet",
    guid="{ETH}",
    description="Realtek PCIe GbE Family Controller",
    kind="ethernet",
    is_hardware=True,
    is_connected=True,
)


class Env:
    """Fake wall + monotonic clocks advanced by the fake stop signal's wait()."""

    def __init__(self, fake: FakePlatform) -> None:
        self.fake = fake
        self.now = T0
        self.mono = 100.0
        self.iteration = 0
        self.actions: dict[int, list] = {}
        self.calls: dict[str, int] = {"rollup": 0, "alerts": 0, "retention": 0}
        fake.on_wait = self._on_wait

    def at(self, iteration: int, action) -> None:
        self.actions.setdefault(iteration, []).append(action)

    def _on_wait(self, timeout_s: float) -> None:
        self.iteration += 1
        self.now += int(timeout_s)
        self.mono += timeout_s
        self.fake.add_traffic(ETH.guid, rx=100, tx=10)
        for action in self.actions.get(self.iteration, []):
            action()
        if self.iteration > 50:
            raise AssertionError("loop did not stop")


@pytest.fixture
def fake() -> FakePlatform:
    p = FakePlatform(boot_time_utc=T0 - 3600)
    p.add_adapter(ETH, rx=1_000, tx=100)
    return p


@pytest.fixture
def env(fake: FakePlatform, monkeypatch: pytest.MonkeyPatch, lagos: ZoneInfo) -> Env:
    e = Env(fake)
    monkeypatch.setattr(timeutil, "now_utc_s", lambda: e.now)
    monkeypatch.setattr(timeutil, "local_zone", lambda refresh=False: lagos)

    def count(name: str):
        def _step(*args, **kwargs) -> None:
            e.calls[name] += 1

        return _step

    monkeypatch.setattr(service, "rollup_step", count("rollup"))
    monkeypatch.setattr(service, "alerts_step", count("alerts"))
    monkeypatch.setattr(service, "retention_step", count("retention"))
    return e


def run(db: sqlite3.Connection, fake: FakePlatform, env: Env, **kwargs) -> int:
    return service.run(db, fake, monotonic=lambda: env.mono, **kwargs)


def sample_total(db: sqlite3.Connection) -> tuple[int, int]:
    row = db.execute("SELECT COALESCE(SUM(rx_bytes),0), COALESCE(SUM(tx_bytes),0) FROM samples")
    return tuple(row.fetchone())


def test_once_runs_one_iteration_and_marks_stopped(db, fake, env) -> None:
    assert run(db, fake, env, once=True) == 0
    assert fake.waits == []
    assert env.calls == {"rollup": 1, "alerts": 1, "retention": 1}
    status = repo.read_collector_status(db)
    assert status.state == "stopped"
    assert status.beat == 2  # one tick + the stopped heartbeat
    assert db.execute("SELECT COUNT(*) FROM collector_state").fetchone()[0] == 1


def test_stop_requested_token_stops_and_is_cleared(db, fake, env) -> None:
    env.at(3, lambda: settings.set_value(db, "stop_requested", "tok-1"))
    assert run(db, fake, env) == 0
    assert env.iteration == 3
    assert settings.get(db, "stop_requested") == ""
    assert repo.read_collector_status(db).state == "stopped"
    # Iterations 2 and 3 sampled traffic added by waits 1 and 2 (wait 3 stopped it first).
    assert sample_total(db) == (200, 20)


def test_stop_signal_wakes_and_stops(db, fake, env) -> None:
    env.at(2, fake.signal_stop)
    run(db, fake, env)
    assert env.iteration == 2
    assert repo.read_collector_status(db).state == "stopped"


def test_stale_stop_token_cleared_at_startup(db, fake, env) -> None:
    settings.set_value(db, "stop_requested", "left-over")
    env.at(2, fake.signal_stop)
    run(db, fake, env)
    assert env.iteration == 2  # did not exit immediately


def test_waits_for_sample_interval(db, fake, env) -> None:
    settings.set_value(db, "sample_interval_s", 30)
    env.at(2, fake.signal_stop)
    run(db, fake, env)
    assert fake.waits == [30, 30]


def test_pause_flag(db, fake, env) -> None:
    states: list[str] = []
    env.at(1, lambda: settings.set_value(db, "collection_paused", "1"))
    env.at(2, lambda: states.append(repo.read_collector_status(db).state))
    env.at(3, lambda: settings.set_value(db, "collection_paused", "0"))
    env.at(4, lambda: states.append(repo.read_collector_status(db).state))
    env.at(5, fake.signal_stop)
    run(db, fake, env)
    assert states == ["paused", "running"]
    # Waits 1..4 each added 100/10; the ones sampled while paused (waits 1, 2) are lost.
    assert sample_total(db) == (200, 20)


def test_reset_request(db, fake, env) -> None:
    def request() -> None:
        assert sample_total(db) != (0, 0)
        settings.set_value(db, "reset_requested", "reset-7")

    env.at(3, request)
    env.at(4, fake.signal_stop)
    run(db, fake, env)
    assert settings.get(db, "reset_done") == "reset-7"
    # Only the tick after the reset survives: the 100/10 added by wait 3.
    assert sample_total(db) == (100, 10)
    assert repo.list_networks(db)  # networks kept by default


def test_reset_request_forget_networks(db, fake, env) -> None:
    def request() -> None:
        settings.set_value(db, "reset_forget_networks", "1")
        settings.set_value(db, "reset_requested", "reset-8")

    env.at(2, request)
    env.at(3, fake.signal_stop)
    run(db, fake, env)
    assert settings.get(db, "reset_done") == "reset-8"
    assert sample_total(db) == (100, 10)  # counted on recreated rows, no lost interval
    assert len(repo.list_networks(db)) == 1


def test_failing_step_never_kills_loop(db, fake, env, monkeypatch) -> None:
    def broken(*args, **kwargs) -> None:
        raise RuntimeError("rollup exploded")

    monkeypatch.setattr(service, "rollup_step", broken)
    seen: list[str | None] = []
    env.at(2, lambda: seen.append(repo.read_collector_status(db).last_error))
    env.at(4, fake.signal_stop)
    assert run(db, fake, env) == 0
    assert env.iteration == 4
    assert env.calls["alerts"] == 4  # later steps still ran every iteration
    assert seen and seen[0] and "rollup exploded" in seen[0]


def test_failing_sample_still_beats_with_error(db, fake, env) -> None:
    fake.snapshot_error = OSError("GetIfTable2 failed")
    beats: list[int] = []
    errors: list[str | None] = []

    def record() -> None:
        status = repo.read_collector_status(db)
        beats.append(status.beat)
        errors.append(status.last_error)

    env.at(1, record)
    env.at(2, record)
    env.at(3, lambda: setattr(fake, "snapshot_error", None))
    env.at(5, fake.signal_stop)
    run(db, fake, env)
    assert beats[1] > beats[0]
    assert errors[0] and "GetIfTable2" in errors[0]
    assert env.calls["rollup"] == 5
    assert sample_total(db) != (0, 0)  # recovered after the error cleared


def test_settings_read_failure_does_not_kill_loop(db, fake, env, monkeypatch) -> None:
    real_get = settings.get
    failures = {"n": 0}

    def flaky_get(conn, key):
        if key == "stop_requested" and failures["n"] < 2:
            failures["n"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real_get(conn, key)

    monkeypatch.setattr(service.settings, "get", flaky_get)
    env.at(4, fake.signal_stop)
    assert run(db, fake, env) == 0
    assert env.iteration == 4


def test_retention_runs_hourly(db, fake, env) -> None:
    settings.set_value(db, "sample_interval_s", 60)
    env.at(70, fake.signal_stop)
    env.fake.on_wait = None

    def on_wait(timeout_s: float) -> None:
        env.iteration += 1
        env.now += 60
        env.mono += 60
        if env.iteration == 70:
            fake.signal_stop()

    fake.on_wait = on_wait
    run(db, fake, env)
    assert env.iteration == 70
    assert env.calls["retention"] == 2  # at start and after 60 minutes


def test_lock_released_on_exit(db, fake, env) -> None:
    lock = fake.acquire_collector_lock()
    assert fake.collector_lock_held()
    run(db, fake, env, once=True, lock=lock)
    assert not fake.collector_lock_held()


def test_optional_steps_tolerate_missing_modules(monkeypatch, caplog) -> None:
    monkeypatch.setattr(service, "_warned_missing", set())
    with caplog.at_level(logging.WARNING):
        assert service._optional("x", "bytelog.does_not_exist", "fn") is None
        assert service._optional("x", "bytelog.does_not_exist", "fn") is None
    assert len([r for r in caplog.records if "unavailable" in r.message]) == 1


# --- __main__ ------------------------------------------------------------------------


@pytest.fixture
def quiet_main(monkeypatch, fake, lagos):
    """Run main() against the fake platform without touching global logging/hooks."""
    import bytelog.logging_setup
    import bytelog.platform

    monkeypatch.setattr(
        bytelog.logging_setup, "setup_logging", lambda *a, **k: logging.getLogger("t")
    )
    monkeypatch.setattr(bytelog.platform, "get_platform", lambda: fake)
    monkeypatch.setattr(service, "install_crash_handlers", lambda: None)
    monkeypatch.setattr(timeutil, "local_zone", lambda refresh=False: lagos)
    for name in ("rollup_step", "alerts_step", "retention_step"):
        monkeypatch.setattr(service, name, lambda *a, **k: None)
    return fake


def test_main_once(tmp_path: Path, quiet_main) -> None:
    data = tmp_path / "data"
    assert collector_main.main(["--data-dir", str(data), "--once"]) == 0
    conn = open_db(data / "bytelog.db")
    try:
        assert repo.read_collector_status(conn).state == "stopped"
        assert conn.execute("SELECT COUNT(*) FROM adapters").fetchone()[0] == 1
    finally:
        conn.close()
    assert not quiet_main.lock_held


def test_main_exits_when_lock_held(tmp_path: Path, quiet_main) -> None:
    quiet_main.lock_held = True
    assert collector_main.main(["--data-dir", str(tmp_path / "d"), "--once"]) == 0
    assert not (tmp_path / "d" / "bytelog.db").exists()


def test_main_stop(tmp_path: Path, quiet_main) -> None:
    data = tmp_path / "data"
    assert collector_main.main(["--data-dir", str(data), "--stop"]) == 0
    assert quiet_main.stop_event.is_set()
    conn = open_db(data / "bytelog.db")
    try:
        assert settings.get(conn, "stop_requested") != ""
    finally:
        conn.close()


def _unmappable_zone() -> None:
    from zoneinfo import ZoneInfoNotFoundError

    raise ZoneInfoNotFoundError("Cen. Australia Standard Time")


def test_unmappable_windows_zone_falls_back_to_fixed_offset(monkeypatch) -> None:
    import tzlocal

    monkeypatch.setattr(tzlocal, "reload_localzone", _unmappable_zone)
    monkeypatch.setattr(tzlocal, "get_localzone", _unmappable_zone)
    zone = REAL_LOCAL_ZONE(refresh=True)
    from datetime import datetime

    assert zone.utcoffset(datetime.now()) == datetime.now().astimezone().utcoffset()


def test_unmappable_windows_zone_does_not_stop_collector(db, fake, env, monkeypatch) -> None:
    import tzlocal

    monkeypatch.setattr(timeutil, "local_zone", REAL_LOCAL_ZONE)
    monkeypatch.setattr(tzlocal, "reload_localzone", _unmappable_zone)
    monkeypatch.setattr(tzlocal, "get_localzone", _unmappable_zone)
    env.at(3, lambda: settings.set_value(db, "stop_requested", "tok"))
    assert run(db, fake, env) == 0
    assert sample_total(db) == (200, 20)  # ticks 1 and 2 counted; stopped at iteration 3
    assert repo.read_collector_status(db).state == "stopped"
