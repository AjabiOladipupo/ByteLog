"""Smoke tests against the real Windows APIs (skipped elsewhere). Read-only: no tasks,
registry values or toasts are created."""

from __future__ import annotations

import sys
import threading
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

TEST_PREFIX = "ByteLogTest.Collector"


@pytest.fixture(scope="module")
def platform():
    from bytelog.platform.windows import WindowsPlatform

    return WindowsPlatform(object_prefix=TEST_PREFIX)


def test_snapshot_plausible(platform) -> None:
    snap = platform.snapshot()
    assert snap.adapters, "no network interfaces reported"
    assert set(snap.counters) == set(snap.adapters)
    for guid, info in snap.adapters.items():
        assert info.guid == guid
        assert info.kind in ("wifi", "ethernet", "cellular", "other")
        c = snap.counters[guid]
        assert c.rx >= 0 and c.tx >= 0
    # Loopback and NDIS filter layers are not listed.
    assert not any("loopback" in a.description.lower() for a in snap.adapters.values())
    assert not any("lightweight filter" in a.description.lower() for a in snap.adapters.values())
    # Counters only move forwards between two quick reads.
    again = platform.snapshot()
    for guid, c in again.counters.items():
        if guid in snap.counters:
            assert c.rx >= snap.counters[guid].rx


def test_native_matches_psutil_counters(platform) -> None:
    from bytelog.platform.windows import read_if_table, read_psutil

    native = read_if_table()
    fallback = read_psutil()
    by_name = {a.name: g for g, a in native.adapters.items()}
    common = [n for n in fallback.adapters if n in by_name]
    assert common
    for name in common:
        assert abs(native.counters[by_name[name]].rx - fallback.counters[name].rx) < 50_000_000


def test_boot_time_plausible(platform) -> None:
    import time

    boot = platform.boot_time_utc()
    assert 0 < boot < time.time()


def test_wifi_ssid_does_not_raise(platform) -> None:
    for info in platform.snapshot().adapters.values():
        if info.kind == "wifi" and info.is_hardware and info.is_connected:
            ssid = platform.wifi_ssid(info)
            assert ssid is None or (isinstance(ssid, bytes) and 0 < len(ssid) <= 32)


def test_lock_and_stop_signal(platform) -> None:
    def held_from_other_thread() -> bool:
        result: list[bool] = []
        t = threading.Thread(target=lambda: result.append(platform.collector_lock_held()))
        t.start()
        t.join()
        return result[0]

    assert not held_from_other_thread()
    lock = platform.acquire_collector_lock()
    assert lock is not None
    try:
        assert platform.acquire_collector_lock() is None
        assert held_from_other_thread()
    finally:
        lock.release()
    assert not held_from_other_thread()

    signal = platform.open_stop_signal()
    try:
        assert signal.wait(0.05) is False
        platform.signal_stop()
        assert signal.wait(2) is True
        assert signal.wait(0.05) is False  # auto-reset
    finally:
        signal.close()


def test_task_xml_well_formed() -> None:
    from bytelog.platform.windows import build_task_xml

    xml = build_task_xml(
        [r"C:\Program Files\ByteLog & Co\pythonw.exe", "-m", "bytelog.collector"], "PC\\user"
    )
    root = ET.fromstring(xml.split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:Exec/t:Command", ns).text.endswith("pythonw.exe")
    assert root.find(".//t:Exec/t:Arguments", ns).text == "-m bytelog.collector"
    assert root.find(".//t:LogonTrigger/t:UserId", ns).text == "PC\\user"
    assert root.find(".//t:ExecutionTimeLimit", ns).text == "PT0S"
    assert root.find(".//t:StopIfGoingOnBatteries", ns).text == "false"
    assert root.find(".//t:Principal/t:LogonType", ns).text == "InteractiveToken"
    assert root.find(".//t:Principal/t:RunLevel", ns).text == "LeastPrivilege"
    no_args = ET.fromstring(build_task_xml([r"C:\B\ByteLogCollector.exe"], "u").split("?>", 1)[1])
    assert no_args.find(".//t:Exec/t:Arguments", ns) is None


def test_task_xml_command_round_trip() -> None:
    from bytelog.platform.windows import build_task_xml, task_xml_command

    exe = r"C:\Program Files\ByteLog & Co\ByteLogCollector.exe"
    xml = build_task_xml([exe], "PC\\user")
    assert task_xml_command(xml.encode("utf-16")) == exe  # schtasks /xml on most builds
    assert task_xml_command(xml.encode("mbcs")) == exe
    assert task_xml_command(b"ERROR: not found") is None


def test_first_argument() -> None:
    from bytelog.platform.windows import first_argument

    assert first_argument('"C:\\A B\\c.exe" --data-dir x') == "C:\\A B\\c.exe"
    assert first_argument("C:\\AB\\c.exe --once") == "C:\\AB\\c.exe"


def test_autostart_is_current_compares_paths(monkeypatch) -> None:
    from bytelog.platform import windows

    monkeypatch.setattr(windows, "collector_command", lambda: [r"C:\New\ByteLogCollector.exe"])
    plat = windows.WindowsPlatform(object_prefix=TEST_PREFIX)
    monkeypatch.setattr(windows, "registered_autostart_exe", lambda: None)
    assert not plat.autostart_is_current()
    monkeypatch.setattr(windows, "registered_autostart_exe", lambda: r"C:\Old\ByteLogCollector.exe")
    assert not plat.autostart_is_current()
    monkeypatch.setattr(windows, "registered_autostart_exe", lambda: r"c:\new\bytelogcollector.EXE")
    assert plat.autostart_is_current()


def test_collector_command_passes_data_dir(monkeypatch, tmp_path) -> None:
    from bytelog.platform.windows import collector_command

    monkeypatch.setenv("BYTELOG_HOME", str(tmp_path))
    cmd = collector_command()
    assert cmd[-2:] == ["--data-dir", str(tmp_path)]
    assert "bytelog.collector" in cmd


def _info(name: str, guid: str, **kw) -> object:
    from bytelog.platform.base import AdapterInfo

    fields = {
        "description": "Intel(R) Wi-Fi 6 AX201",
        "kind": "wifi",
        "is_hardware": True,
        "is_connected": True,
        "speed_bps": 866_000_000,
    }
    fields.update(kw)
    return AdapterInfo(name=name, guid=guid, **fields)


def test_psutil_fallback_keeps_guid_identity(monkeypatch) -> None:
    """A GetIfTable2 failure must not turn adapters into name-keyed new ones (that made
    the GUID baselines count the same traffic again after a restart)."""
    from bytelog.platform import windows
    from bytelog.platform.base import Counters, Snapshot

    guid = "{11111111-2222-3333-4444-555555555555}"
    native = Snapshot({guid: _info("Wi-Fi", guid)}, {guid: Counters(1_000, 100)})
    by_name = Snapshot(
        {
            "Wi-Fi": _info("Wi-Fi", "Wi-Fi", description="", speed_bps=None),
            "Mystery": _info("Mystery", "Mystery", description=""),
        },
        {"Wi-Fi": Counters(5_000, 500), "Mystery": Counters(1, 1)},
    )
    state = {"fail": False, "native_calls": 0, "mono": 1000.0}

    def read_if_table() -> Snapshot:
        state["native_calls"] += 1
        if state["fail"]:
            raise OSError(87, "GetIfTable2 failed")
        return native

    monkeypatch.setattr(windows, "read_if_table", read_if_table)
    monkeypatch.setattr(windows, "read_psutil", lambda: by_name)
    monkeypatch.setattr(windows, "alias_to_guid", lambda alias: None)
    monkeypatch.setattr(windows.time, "monotonic", lambda: state["mono"])
    p = windows.WindowsPlatform(object_prefix=TEST_PREFIX)
    assert p.snapshot() is native

    state["fail"] = True
    snap = p.snapshot()
    assert list(snap.adapters) == [guid]  # unmappable "Mystery" is skipped, not invented
    assert snap.counters[guid] == Counters(5_000, 500)
    info = snap.adapters[guid]
    assert (info.description, info.speed_bps, info.kind) == (
        "Intel(R) Wi-Fi 6 AX201",
        866_000_000,
        "wifi",
    )

    # The native call is retried periodically, not disabled for the process lifetime.
    calls = state["native_calls"]
    state["mono"] += 10
    p.snapshot()
    assert state["native_calls"] == calls
    state["fail"] = False
    state["mono"] += windows.NATIVE_RETRY_S
    assert p.snapshot() is native


def test_remap_resolves_unknown_alias_to_guid() -> None:
    from bytelog.platform.base import Counters, Snapshot
    from bytelog.platform.windows import remap_to_guids

    snap = Snapshot(
        {"Ethernet": _info("Ethernet", "Ethernet", kind="ethernet")}, {"Ethernet": Counters(7, 8)}
    )
    out = remap_to_guids(snap, {}, lambda alias: "{E}" if alias == "Ethernet" else None)
    assert out.adapters["{E}"].guid == "{E}"
    assert out.counters == {"{E}": Counters(7, 8)}


def test_alias_to_guid_matches_native_table() -> None:
    from bytelog.platform.windows import alias_to_guid, read_if_table

    native = read_if_table()
    for guid, info in native.adapters.items():
        if info.is_hardware:
            assert alias_to_guid(info.name) == guid
    assert alias_to_guid("no such adapter ☃") is None


def test_wifi_ssid_falls_back_to_profile_when_wlan_lacks_adapter(monkeypatch) -> None:
    from bytelog.platform import windows

    p = windows.WindowsPlatform(object_prefix=TEST_PREFIX)
    monkeypatch.setattr(p._wlan, "ssids", lambda: {"{OTHER}": b"x"})
    monkeypatch.setattr(windows, "profile_names", lambda: {"Wi-Fi": "HomeNet"})
    assert p.wifi_ssid(_info("Wi-Fi", "{MINE}")) == b"HomeNet"
