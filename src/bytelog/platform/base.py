"""OS abstraction. Everything OS-specific lives behind :class:`Platform`.

``windows.py`` is the real implementation; ``fake.py`` drives tests with scripted
counter sequences. A future ``macos.py`` / ``linux.py`` only has to implement this.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

AdapterKind = Literal["wifi", "ethernet", "cellular", "other"]


@dataclass(frozen=True)
class AdapterInfo:
    """One network interface as the OS reports it at a given moment."""

    name: str  # friendly name / alias, e.g. "Wi-Fi", "Ethernet 2"
    guid: str  # stable identity (Windows InterfaceGuid); falls back to name elsewhere
    description: str  # driver description, e.g. "Intel(R) Wi-Fi 6 AX201 160MHz"
    kind: AdapterKind
    is_hardware: bool  # OS says this is a physical (hardware) interface
    is_connected: bool  # media connected / oper status up
    speed_bps: int | None = None  # link speed, used for the plausibility cap


@dataclass(frozen=True)
class Counters:
    """Raw cumulative byte counters for one adapter (never adjusted for wraps)."""

    rx: int
    tx: int


@dataclass(frozen=True)
class Snapshot:
    """One consistent read of every interface: metadata plus raw counters, keyed by GUID."""

    adapters: dict[str, AdapterInfo]
    counters: dict[str, Counters]


class InstanceLock(Protocol):
    def release(self) -> None: ...


class StopSignal(Protocol):
    """Cross-process "please stop" signal (a named event on Windows)."""

    def wait(self, timeout_s: float) -> bool:
        """Block up to ``timeout_s``; True if a stop was signalled."""
        ...

    def close(self) -> None: ...


class Platform(Protocol):
    # How long callers may reuse a successful ``wifi_ssid`` result while the adapter stays
    # connected. 0 means the lookup is cheap (the implementation caches any slow
    # fallback itself), so callers ask on every sample and notice a quick network switch.
    ssid_refresh_s: int

    def snapshot(self) -> Snapshot:
        """Read all adapters and their raw counters in one go."""
        ...

    def wifi_ssid(self, adapter: AdapterInfo) -> bytes | None:
        """Raw SSID bytes of the network ``adapter`` is connected to; None if unknown.

        Implementations may be slow or permission-gated (Windows 11 24H2 Location);
        callers cache the result for ``ssid_refresh_s`` and re-query when the connection
        changes.
        """
        ...

    def boot_time_utc(self) -> int: ...

    def acquire_collector_lock(self) -> InstanceLock | None:
        """Single-instance lock for the collector; None if another one holds it."""
        ...

    def collector_lock_held(self) -> bool:
        """True if some collector process currently holds the lock (for the dashboard)."""
        ...

    def open_stop_signal(self) -> StopSignal: ...

    def signal_stop(self) -> None:
        """Ask a running collector to stop now (used by Settings and --stop)."""
        ...

    def notify(self, title: str, message: str) -> bool:
        """Show a native desktop notification. Returns False if it could not be shown."""
        ...

    def launch_collector(self) -> None:
        """Start the collector detached from the calling process (dev and frozen)."""
        ...

    def autostart_enabled(self) -> bool: ...

    def autostart_is_current(self) -> bool:
        """True if start-at-login is registered and starts *this* installation's collector.

        False when it is missing or points at another copy (the app was moved, reinstalled
        elsewhere, or first run from a build folder), so callers can re-register it.
        """
        ...

    def set_autostart(self, enabled: bool) -> None:
        """Register/unregister the collector to start at user login."""
        ...

    def open_path(self, path: Path) -> None:
        """Open a folder or file with the OS shell."""
        ...
