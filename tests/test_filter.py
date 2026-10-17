from __future__ import annotations

import pytest

from bytelog.collector.filter import matching_pattern, should_count
from bytelog.platform.base import AdapterInfo
from bytelog.storage.settings import DEFAULT_EXCLUDE_PATTERNS

PATTERNS = list(DEFAULT_EXCLUDE_PATTERNS)


def adapter(
    name: str, description: str, kind: str = "ethernet", hardware: bool = True
) -> AdapterInfo:
    return AdapterInfo(
        name=name,
        guid="{" + name + "}",
        description=description,
        kind=kind,  # type: ignore[arg-type]
        is_hardware=hardware,
        is_connected=True,
    )


INTEL_WIFI = adapter("Wi-Fi", "Intel(R) Wireless-AC 9560 160MHz", "wifi")
REALTEK = adapter("Ethernet", "Realtek PCIe GbE Family Controller")
USB_TETHER = adapter("Ethernet 3", "Remote NDIS based Internet Sharing Device")
LTE = adapter("Cellular", "Qualcomm Snapdragon X12 LTE-A", "cellular")

EXCLUDED = [
    # Hyper-V / WSL switches report as Ethernet and are not hardware...
    adapter("vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter", hardware=False),
    adapter("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter #2", hardware=False),
    # ...but the patterns also catch them if a driver claims to be hardware.
    adapter("vEthernet (WSL (Hyper-V firewall))", "Hyper-V Virtual Ethernet Adapter #3"),
    adapter("Ethernet 4", "TAP-Windows Adapter V9"),
    adapter("OpenVPN Wintun", "Wintun Userspace Tunnel"),
    adapter("ProtonVPN", "WireGuard Tunnel"),
    adapter("Ethernet 5", "Cisco AnyConnect Secure Mobility Client Virtual Miniport Adapter"),
    adapter("Bluetooth Network Connection", "Bluetooth Device (Personal Area Network)", "other"),
    adapter("Bluetooth Network Connection 2", "Bluetooth Device (PAN)"),
    adapter("vEthernet (DockerNAT)", "Docker Desktop Virtual Ethernet"),
    adapter("VMware Network Adapter VMnet8", "VMware Virtual Ethernet Adapter for VMnet8"),
    adapter("VirtualBox Host-Only Network", "VirtualBox Host-Only Ethernet Adapter"),
    adapter("Loopback Pseudo-Interface 1", "Software Loopback Interface 1", "other", False),
    adapter("Local Area Connection* 1", "Microsoft Wi-Fi Direct Virtual Adapter", "wifi", False),
    adapter("Local Area Connection* 8", "WAN Miniport (IP)", hardware=False),
    adapter("Ethernet (Kernel Debugger)", "Microsoft Kernel Debug Network Adapter"),
    adapter("Tailscale", "Tailscale Tunnel"),
]


@pytest.mark.parametrize("info", [INTEL_WIFI, REALTEK, USB_TETHER, LTE], ids=lambda a: a.name)
def test_physical_adapters_counted(info: AdapterInfo) -> None:
    assert should_count(info, PATTERNS, None)


@pytest.mark.parametrize("info", EXCLUDED, ids=lambda a: a.description)
def test_virtual_adapters_excluded(info: AdapterInfo) -> None:
    assert not should_count(info, PATTERNS, None)


def test_patterns_case_insensitive_on_name_and_description() -> None:
    assert matching_pattern(adapter("MY-VPN", "Some NIC"), ["vpn"]) == "vpn"
    assert matching_pattern(adapter("Ethernet", "Hyper-V thing"), ["HYPER-V"]) == "HYPER-V"
    assert matching_pattern(REALTEK, ["vpn", "  ", ""]) is None


def test_kind_other_not_counted_by_default() -> None:
    assert not should_count(adapter("Thing", "Some hardware", "other"), [], None)


def test_override_includes_excluded_adapter() -> None:
    vpn = adapter("Ethernet 4", "TAP-Windows Adapter V9", hardware=False)
    assert should_count(vpn, PATTERNS, True)


def test_override_excludes_physical_adapter() -> None:
    assert not should_count(INTEL_WIFI, PATTERNS, False)


def test_user_patterns_replace_defaults() -> None:
    assert should_count(adapter("vEthernet", "Hyper-V Virtual"), [], None)
    assert not should_count(REALTEK, ["realtek"], None)
