from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

import pytest

from bytelog.collector import sampler as sampler_mod
from bytelog.collector.delta import FLAG_CLOCK, FLAG_GAP, FLAG_RESET, FLAG_SSID_UNKNOWN
from bytelog.collector.sampler import Sampler
from bytelog.platform.base import AdapterInfo
from bytelog.platform.fake import FakePlatform
from bytelog.storage import maintenance, repo

T0 = 1_800_000_000
WIFI = AdapterInfo(
    name="Wi-Fi",
    guid="{WIFI}",
    description="Intel(R) Wireless-AC 9560 160MHz",
    kind="wifi",
    is_hardware=True,
    is_connected=True,
    speed_bps=144_400_000,
)
ETH = AdapterInfo(
    name="Ethernet",
    guid="{ETH}",
    description="Realtek PCIe GbE Family Controller",
    kind="ethernet",
    is_hardware=True,
    is_connected=True,
    speed_bps=1_000_000_000,
)
VETH = AdapterInfo(
    name="vEthernet (WSL)",
    guid="{VETH}",
    description="Hyper-V Virtual Ethernet Adapter",
    kind="ethernet",
    is_hardware=False,
    is_connected=True,
)


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self.mono = 1000.0

    def advance(self, seconds: int) -> int:
        self.now += seconds
        self.mono += seconds
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake() -> FakePlatform:
    p = FakePlatform(boot_time_utc=T0 - 3600)
    p.add_adapter(WIFI, rx=10_000_000, tx=1_000_000, ssid=b"HomeNet")
    p.add_adapter(ETH, rx=500, tx=100)
    return p


def make(db: sqlite3.Connection, fake: FakePlatform, clock: Clock, lagos: ZoneInfo) -> Sampler:
    return Sampler(db, fake, lambda: lagos, monotonic=lambda: clock.mono, pid=4242)


def samples(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT s.*, n.kind AS nkind, n.ssid, n.identity, n.adapter_name "
        "FROM samples s JOIN networks n ON n.id = s.network_id ORDER BY s.id"
    ).fetchall()


def totals_by_network(db: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    rows = db.execute(
        "SELECT COALESCE(n.ssid, n.identity) AS label, SUM(rx_bytes), SUM(tx_bytes) "
        "FROM samples s JOIN networks n ON n.id = s.network_id GROUP BY n.id"
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_first_tick_is_baseline_only_then_counts_both_adapters(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    assert s.tick(clock.now) == 0
    assert db.execute("SELECT COUNT(*) FROM collector_state").fetchone()[0] == 2

    fake.add_traffic(WIFI.guid, rx=4_000, tx=1_000)
    fake.add_traffic(ETH.guid, rx=300, tx=30)
    assert s.tick(clock.advance(10)) == 2

    rows = samples(db)
    by_kind = {r["nkind"]: r for r in rows}
    assert (by_kind["wifi"]["rx_bytes"], by_kind["wifi"]["tx_bytes"]) == (4_000, 1_000)
    assert by_kind["wifi"]["ssid"] == "HomeNet"
    assert by_kind["wifi"]["identity"] == "ssid:" + b"HomeNet".hex()
    assert (by_kind["ethernet"]["rx_bytes"], by_kind["ethernet"]["tx_bytes"]) == (300, 30)
    assert by_kind["ethernet"]["identity"] == ETH.guid
    assert by_kind["ethernet"]["adapter_name"] == "Ethernet"
    for r in rows:
        assert r["ts_utc"] == clock.now
        assert r["span_s"] == 10
        assert r["utc_offset_s"] == 3600  # Africa/Lagos
        assert r["flags"] == 0

    status = repo.read_collector_status(db)
    assert status.beat == 2
    assert status.state == "running"
    assert status.pid == 4242
    assert status.heartbeat_utc == clock.now
    assert status.started_utc == T0


def test_zero_deltas_write_no_rows(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    assert s.tick(clock.advance(10)) == 0
    fake.add_traffic(ETH.guid, rx=1)
    assert s.tick(clock.advance(10)) == 1


def test_ssid_cached_and_requeried_on_reconnect(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    for _ in range(5):
        fake.add_traffic(WIFI.guid, rx=100)
        s.tick(clock.advance(10))
    assert len(fake.ssid_queries) == 1

    # Disconnect, move to the MiFi, reconnect: the SSID is looked up again.
    fake.set_connected(WIFI.guid, False)
    s.tick(clock.advance(10))
    fake.set_connected(WIFI.guid, True)
    fake.set_ssid(WIFI.guid, b"MTN-MiFi")
    fake.add_traffic(WIFI.guid, rx=7_000, tx=700)
    s.tick(clock.advance(10))
    assert len(fake.ssid_queries) == 2
    assert totals_by_network(db) == {"HomeNet": (500, 0), "MTN-MiFi": (7_000, 700)}


def test_ssid_requeried_every_five_minutes(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    for _ in range(31):
        fake.add_traffic(WIFI.guid, rx=10)
        s.tick(clock.advance(10))
    assert len(fake.ssid_queries) == 2


def test_ssid_requeried_after_gap(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(WIFI.guid, rx=10)
    s.tick(clock.advance(10))
    fake.set_ssid(WIFI.guid, b"Hotel")
    fake.add_traffic(WIFI.guid, rx=99)
    s.tick(clock.advance(3 * 3600))
    row = samples(db)[-1]
    assert row["ssid"] == "Hotel"
    assert row["flags"] & FLAG_GAP
    assert row["span_s"] == 3 * 3600


def test_ssid_failure_reuses_last_network_then_unknown(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(WIFI.guid, rx=10)
    s.tick(clock.advance(10))

    fake.ssid_error = PermissionError("location off")
    fake.set_connected(WIFI.guid, False)
    s.tick(clock.advance(10))
    fake.set_connected(WIFI.guid, True)
    for _ in range(3):  # within 5 minutes: still HomeNet
        fake.add_traffic(WIFI.guid, rx=20)
        s.tick(clock.advance(10))
    assert totals_by_network(db) == {"HomeNet": (70, 0)}

    clock.advance(300)
    fake.add_traffic(WIFI.guid, rx=5)
    s.tick(clock.advance(10))
    row = samples(db)[-1]
    assert row["nkind"] == "wifi_unknown"
    assert row["identity"] == WIFI.guid
    assert row["flags"] & FLAG_SSID_UNKNOWN


def test_non_utf8_ssid(db, fake, clock, lagos) -> None:
    fake.set_ssid(WIFI.guid, b"Caf\xe9")
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(WIFI.guid, rx=10)
    s.tick(clock.advance(10))
    row = samples(db)[-1]
    assert row["identity"] == "ssid:436166e9"
    assert row["ssid"] == "Caf�"


def test_disconnect_tail_bytes_go_to_last_network(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(WIFI.guid, rx=10)
    s.tick(clock.advance(10))
    fake.set_connected(WIFI.guid, False)
    fake.add_traffic(WIFI.guid, rx=33)
    s.tick(clock.advance(10))
    assert totals_by_network(db) == {"HomeNet": (43, 0)}


def test_pause_advances_baselines_without_samples(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(ETH.guid, rx=1_000_000)
    assert s.tick(clock.advance(10), paused=True) == 0
    assert repo.read_collector_status(db).state == "paused"
    assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0

    fake.add_traffic(ETH.guid, rx=5)
    assert s.tick(clock.advance(10)) == 1
    assert totals_by_network(db) == {ETH.guid: (5, 0)}
    assert repo.read_collector_status(db).state == "running"


def test_restart_continues_without_double_count(db, fake, clock, lagos) -> None:
    first = make(db, fake, clock, lagos)
    first.tick(clock.now)
    fake.add_traffic(ETH.guid, rx=1_000)
    first.tick(clock.advance(10))

    fake.add_traffic(ETH.guid, rx=250)  # traffic while the collector restarts
    second = make(db, fake, clock, lagos)
    assert second.tick(clock.advance(30)) == 1
    fake.add_traffic(ETH.guid, rx=50)
    second.tick(clock.advance(10))
    assert totals_by_network(db) == {ETH.guid: (1_300, 0)}


def test_reboot_detected_and_post_boot_bytes_counted(db, fake, clock, lagos) -> None:
    first = make(db, fake, clock, lagos)
    fake.set_counters(ETH.guid, 9_000_000, 900_000)
    first.tick(clock.now)

    # Reboot: counters restart from zero and boot time moves.
    clock.advance(3600)
    fake.boot_time = clock.now - 300
    fake.set_counters(ETH.guid, 40_000, 4_000)
    second = make(db, fake, clock, lagos)
    second.tick(clock.now)
    row = samples(db)[-1]
    assert (row["rx_bytes"], row["tx_bytes"]) == (40_000, 4_000)
    assert row["flags"] & FLAG_RESET
    assert row["flags"] & FLAG_GAP


def test_reboot_with_higher_counters_detected_by_boot_time(db, fake, clock, lagos) -> None:
    first = make(db, fake, clock, lagos)
    fake.set_counters(ETH.guid, 1_000, 100)
    first.tick(clock.now)
    clock.advance(3600)
    fake.boot_time = clock.now - 600
    fake.set_counters(ETH.guid, 80_000, 8_000)
    second = make(db, fake, clock, lagos)
    second.tick(clock.now)
    row = samples(db)[-1]
    assert (row["rx_bytes"], row["tx_bytes"]) == (80_000, 8_000)
    assert row["flags"] & FLAG_RESET


def test_excluded_adapter_baseline_kept_so_including_does_not_backfill(
    db, fake, clock, lagos
) -> None:
    fake.add_adapter(VETH, rx=50_000_000_000, tx=1_000)
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(VETH.guid, rx=999)
    s.tick(clock.advance(10))
    assert totals_by_network(db) == {}

    adapter_id = db.execute("SELECT id FROM adapters WHERE guid=?", (VETH.guid,)).fetchone()[0]
    repo.set_adapter_override(db, adapter_id, True)
    fake.add_traffic(VETH.guid, rx=500, tx=5)
    s.tick(clock.advance(10))
    assert totals_by_network(db) == {VETH.guid: (500, 5)}


def test_failed_commit_keeps_old_baselines(db, fake, clock, lagos, monkeypatch) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)

    real = repo.write_heartbeat
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(sampler_mod.repo, "write_heartbeat", flaky)
    fake.add_traffic(ETH.guid, rx=100)
    with pytest.raises(sqlite3.OperationalError):
        s.tick(clock.advance(10))
    assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0

    fake.add_traffic(ETH.guid, rx=20)
    s.tick(clock.advance(10))
    row = samples(db)[-1]
    assert row["rx_bytes"] == 120
    assert row["span_s"] == 20


def test_ethernet_rename_keeps_network(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(ETH.guid, rx=1)
    s.tick(clock.advance(10))
    fake.rename_adapter(ETH.guid, "Office LAN")
    fake.add_traffic(ETH.guid, rx=2)
    s.tick(clock.advance(10))
    networks = [n for n in repo.list_networks(db) if n.kind == "ethernet"]
    assert len(networks) == 1
    assert networks[0].adapter_name == "Office LAN"
    adapters = {a.guid: a for a in repo.list_adapters(db)}
    assert adapters[ETH.guid].name == "Office LAN"


def test_clock_back_stores_true_time_and_flag(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    clock.mono += 10
    clock.now -= 3600  # wall clock corrected backwards by an hour
    fake.add_traffic(ETH.guid, rx=77)
    s.tick(clock.now)
    row = samples(db)[-1]
    assert row["ts_utc"] == clock.now
    assert row["span_s"] == 0
    assert row["flags"] & FLAG_CLOCK
    assert row["rx_bytes"] == 77


def test_forget_reset_while_running_recreates_rows(db, fake, clock, lagos) -> None:
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(WIFI.guid, rx=10)
    s.tick(clock.advance(10))

    maintenance.reset_usage(db, forget_networks_and_plans=True)
    s.reset_caches()
    assert db.execute("SELECT COUNT(*) FROM adapters").fetchone()[0] == 0

    fake.add_traffic(WIFI.guid, rx=60)
    assert s.tick(clock.advance(10)) == 1
    assert totals_by_network(db) == {"HomeNet": (60, 0)}
    assert db.execute("SELECT COUNT(*) FROM collector_state").fetchone()[0] == 2


def test_cheap_ssid_lookup_catches_quick_network_switch(db, fake, clock, lagos) -> None:
    # Windows' native WLAN query is cheap, so it is asked on every Wi-Fi sample: a
    # switch that drops the link for less than one interval is still noticed.
    fake.ssid_refresh_s = 0
    s = make(db, fake, clock, lagos)
    s.tick(clock.now)
    fake.add_traffic(WIFI.guid, rx=100)
    s.tick(clock.advance(10))
    fake.set_ssid(WIFI.guid, b"MTN-MiFi")  # never seen disconnected by a tick
    fake.add_traffic(WIFI.guid, rx=7_000, tx=700)
    s.tick(clock.advance(10))
    assert totals_by_network(db) == {"HomeNet": (100, 0), "MTN-MiFi": (7_000, 700)}
