"""Which adapters count towards usage.

Only physical Wi-Fi, Ethernet and cellular adapters are counted by default. VPN tunnels,
Hyper-V/WSL/Docker switches and other virtual adapters carry traffic that the physical
adapter already counts, so counting them too would double count. The OS hardware flag
is the primary signal; the name/description patterns are a safety net for drivers that
wrongly claim to be hardware.
"""

from __future__ import annotations

from collections.abc import Iterable

from bytelog.platform.base import AdapterInfo

COUNTED_KINDS = frozenset({"wifi", "ethernet", "cellular"})


def matching_pattern(adapter: AdapterInfo, patterns: Iterable[str]) -> str | None:
    """First pattern found (case-insensitively) in the adapter's name or description."""
    haystack = f"{adapter.name}\n{adapter.description}".casefold()
    for pattern in patterns:
        needle = pattern.strip().casefold()
        if needle and needle in haystack:
            return pattern
    return None


def default_rule(adapter: AdapterInfo, patterns: Iterable[str]) -> bool:
    return (
        adapter.is_hardware
        and adapter.kind in COUNTED_KINDS
        and matching_pattern(adapter, patterns) is None
    )


def should_count(adapter: AdapterInfo, patterns: list[str], override: bool | None) -> bool:
    """The user's include/exclude override wins; otherwise the default rule applies."""
    if override is not None:
        return override
    return default_rule(adapter, patterns)
