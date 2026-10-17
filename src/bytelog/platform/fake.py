"""Scripted :class:`~bytelog.platform.base.Platform` for tests and demos.

Typical use::

    fake = FakePlatform()
    fake.add_adapter(wifi_info, rx=1_000, tx=200, ssid=b"HomeNet")
    sampler.tick(now_utc=t)                 # first sight: baseline only
    fake.set_counters(wifi_info.guid, 5_000, 900)
    sampler.tick(now_utc=t + 10)            # 4000 down, 700 up

``push_snapshot`` queues exact snapshots that ``snapshot()`` returns first, before falling
back to the live adapter/counter state.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from bytelog.platform.base import AdapterInfo, Counters, Snapshot


class FakeLock:
    def __init__(self, owner: FakePlatform) -> None:
        self._owner = owner
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._owner.lock_held = False


class FakeStopSignal:
    """Auto-reset event like the Windows one. ``on_wait`` runs at the start of every wait."""

    def __init__(self, owner: FakePlatform) -> None:
        self._owner = owner
        self.closed = False

    def wait(self, timeout_s: float) -> bool:
        self._owner.waits.append(timeout_s)
        if self._owner.on_wait is not None:
            self._owner.on_wait(timeout_s)
        event = self._owner.stop_event
        if event.is_set():
            event.clear()
            return True
        return False

    def close(self) -> None:
        self.closed = True


class FakePlatform:
    ssid_refresh_s = 300

    def __init__(self, boot_time_utc: int = 1_700_000_000) -> None:
        self.adapters: dict[str, AdapterInfo] = {}
        self.counters: dict[str, Counters] = {}
        self.ssids: dict[str, bytes | None] = {}
        self.boot_time: int = boot_time_utc
        self.notifications: list[tuple[str, str]] = []
        self.notify_result = True
        self.snapshot_error: Exception | None = None
        self.ssid_error: Exception | None = None
        self.ssid_queries: list[str] = []
        self.waits: list[float] = []
        self.on_wait: Callable[[float], None] | None = None
        self.stop_event = threading.Event()
        self.lock_held = False
        self.launches = 0
        self.autostart = False
        self.autostart_current = True  # False = registered, but for another copy of the app
        self.opened_paths: list[Path] = []
        self._queued: deque[Snapshot] = deque()

    # --- scripting helpers -------------------------------------------------------------

    def add_adapter(
        self, info: AdapterInfo, rx: int = 0, tx: int = 0, ssid: bytes | None = None
    ) -> None:
        self.adapters[info.guid] = info
        self.counters[info.guid] = Counters(rx, tx)
        if ssid is not None:
            self.ssids[info.guid] = ssid

    def remove_adapter(self, guid: str) -> None:
        self.adapters.pop(guid, None)
        self.counters.pop(guid, None)

    def set_counters(self, guid: str, rx: int, tx: int) -> None:
        self.counters[guid] = Counters(rx, tx)

    def add_traffic(self, guid: str, rx: int = 0, tx: int = 0) -> None:
        cur = self.counters[guid]
        self.counters[guid] = Counters(cur.rx + rx, cur.tx + tx)

    def set_connected(self, guid: str, connected: bool) -> None:
        self.adapters[guid] = replace(self.adapters[guid], is_connected=connected)

    def rename_adapter(self, guid: str, name: str) -> None:
        self.adapters[guid] = replace(self.adapters[guid], name=name)

    def set_ssid(self, guid: str, ssid: bytes | None) -> None:
        self.ssids[guid] = ssid

    def push_snapshot(self, snapshot: Snapshot) -> None:
        self._queued.append(snapshot)

    # --- Platform ----------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        if self.snapshot_error is not None:
            raise self.snapshot_error
        if self._queued:
            return self._queued.popleft()
        return Snapshot(adapters=dict(self.adapters), counters=dict(self.counters))

    def wifi_ssid(self, adapter: AdapterInfo) -> bytes | None:
        self.ssid_queries.append(adapter.guid)
        if self.ssid_error is not None:
            raise self.ssid_error
        return self.ssids.get(adapter.guid)

    def boot_time_utc(self) -> int:
        return self.boot_time

    def acquire_collector_lock(self) -> FakeLock | None:
        if self.lock_held:
            return None
        self.lock_held = True
        return FakeLock(self)

    def collector_lock_held(self) -> bool:
        return self.lock_held

    def open_stop_signal(self) -> FakeStopSignal:
        return FakeStopSignal(self)

    def signal_stop(self) -> None:
        self.stop_event.set()

    def notify(self, title: str, message: str) -> bool:
        self.notifications.append((title, message))
        return self.notify_result

    def launch_collector(self) -> None:
        self.launches += 1

    def autostart_enabled(self) -> bool:
        return self.autostart

    def autostart_is_current(self) -> bool:
        return self.autostart and self.autostart_current

    def set_autostart(self, enabled: bool) -> None:
        self.autostart = enabled
        self.autostart_current = enabled

    def open_path(self, path: Path) -> None:
        self.opened_paths.append(path)
