"""Windows implementation of :class:`~bytelog.platform.base.Platform`.

* Adapters and counters: ``GetIfTable2`` (iphlpapi) via ctypes, under 2 ms for every
  interface, with the same 64-bit octet counters psutil reads. Falls back to psutil if the
  native call ever fails.
* SSID: the native WLAN API (``WlanQueryInterface`` opcode 7), joined to the adapter by
  interface GUID. Since Windows 11 24H2 it returns ERROR_ACCESS_DENIED without Location
  consent; then the Network List Manager profile name (``Get-NetConnectionProfile``) is
  used instead, fetched only when the connection changes.
* Single instance: a named mutex ``Global\\ByteLog.Collector.<SID>``; stop requests: a
  named auto-reset event. Both are released by the kernel if the process dies.
* Toasts: Windows-Toasts with the AUMID ``ByteLog.App`` registered under HKCU.
* Autostart: a per-user logon task (``schtasks /create /xml``), falling back to the HKCU
  Run key. Every subprocess runs with ``CREATE_NO_WINDOW`` and a timeout.

No admin rights are needed for any of it.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import getpass
import html
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import psutil

from bytelog.paths import ENV_HOME, is_frozen, resource_path
from bytelog.platform.base import AdapterInfo, AdapterKind, Counters, Snapshot

log = logging.getLogger(__name__)

AUMID = "ByteLog.App"
TASK_NAME = "ByteLog Collector"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ByteLog Collector"
COLLECTOR_EXE = "ByteLogCollector.exe"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
SYNCHRONIZE = 0x00100000
MUTEX_MODIFY_STATE = 0x0001
EVENT_MODIFY_STATE = 0x0002

SUBPROCESS_TIMEOUT_S = 20
PROFILE_CACHE_S = 600
NATIVE_RETRY_S = 60

# MIB_IF_ROW2.Type (IANA ifType)
IF_TYPE_ETHERNET = 6
IF_TYPE_PPP = 23
IF_TYPE_LOOPBACK = 24
IF_TYPE_IEEE80211 = 71
IF_TYPE_TUNNEL = 131
IF_TYPE_WWANPP = 243
IF_TYPE_WWANPP2 = 244
# NDIS_PHYSICAL_MEDIUM
PHYSICAL_MEDIUM_BLUETOOTH = 10
# InterfaceAndOperStatusFlags bits
FLAG_HARDWARE = 0x01
FLAG_FILTER = 0x02
FLAG_ENDPOINT = 0x80
MEDIA_CONNECT_STATE_CONNECTED = 1
IF_OPER_STATUS_UP = 1


# --- structures ----------------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:
        return "{" + str(uuid.UUID(bytes_le=bytes(self))).upper() + "}"


IF_MAX_STRING_SIZE = 256
IF_MAX_PHYS_ADDRESS_LENGTH = 32


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceGuid", GUID),
        ("Alias", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("Description", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int),
        ("MediaType", ctypes.c_int),
        ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int),
        ("DirectionType", ctypes.c_int),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),  # bitfield of BOOLEANs
        ("OperStatus", ctypes.c_int),
        ("AdminStatus", ctypes.c_int),
        ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", GUID),
        ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64),
        ("InUcastPkts", ctypes.c_uint64),
        ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64),
        ("InErrors", ctypes.c_uint64),
        ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64),
        ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64),
        ("OutOctets", ctypes.c_uint64),
        ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64),
        ("OutDiscards", ctypes.c_uint64),
        ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64),
        ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64),
        ("OutQLen", ctypes.c_uint64),
    ]


assert ctypes.sizeof(MIB_IF_ROW2) == 1352, "MIB_IF_ROW2 layout mismatch"


class MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG), ("Table", MIB_IF_ROW2 * 1)]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", wintypes.WCHAR * 256),
        ("isState", ctypes.c_int),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", wintypes.ULONG), ("ucSSID", ctypes.c_ubyte * 32)]


class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", ctypes.c_int),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11PhyType", ctypes.c_int),
        ("uDot11PhyIndex", wintypes.ULONG),
        ("wlanSignalQuality", wintypes.ULONG),
        ("ulRxRate", wintypes.ULONG),
        ("ulTxRate", wintypes.ULONG),
    ]


class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    # Trailing WLAN_SECURITY_ATTRIBUTES omitted: only read through a pointer.
    _fields_ = [
        ("isState", ctypes.c_int),
        ("wlanConnectionMode", ctypes.c_int),
        ("strProfileName", wintypes.WCHAR * 256),
        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
    ]


WLAN_INTF_OPCODE_CURRENT_CONNECTION = 7
WLAN_INTERFACE_STATE_CONNECTED = 1


# --- DLL handles (lazy, so importing this module never fails) -------------------------


@functools.cache
def _kernel32() -> Any:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenMutexW.restype = wintypes.HANDLE
    k.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k.ReleaseMutex.restype = wintypes.BOOL
    k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.SetEvent.argtypes = [wintypes.HANDLE]
    k.SetEvent.restype = wintypes.BOOL
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.LocalFree.argtypes = [wintypes.HLOCAL]
    k.LocalFree.restype = wintypes.HLOCAL
    return k


@functools.cache
def _iphlpapi() -> Any:
    lib = ctypes.WinDLL("iphlpapi")
    lib.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(MIB_IF_TABLE2))]
    lib.GetIfTable2.restype = wintypes.ULONG
    lib.FreeMibTable.argtypes = [wintypes.LPVOID]
    lib.FreeMibTable.restype = None
    lib.ConvertInterfaceAliasToLuid.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_uint64)]
    lib.ConvertInterfaceAliasToLuid.restype = wintypes.DWORD
    lib.ConvertInterfaceLuidToGuid.argtypes = [
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(GUID),
    ]
    lib.ConvertInterfaceLuidToGuid.restype = wintypes.DWORD
    return lib


@functools.cache
def _wlanapi() -> Any:
    lib = ctypes.WinDLL("wlanapi")
    lib.WlanOpenHandle.argtypes = [
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.HANDLE),
    ]
    lib.WlanOpenHandle.restype = wintypes.DWORD
    lib.WlanCloseHandle.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    lib.WlanCloseHandle.restype = wintypes.DWORD
    lib.WlanEnumInterfaces.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.POINTER(ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)),
    ]
    lib.WlanEnumInterfaces.restype = wintypes.DWORD
    lib.WlanQueryInterface.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(GUID),
        ctypes.c_int,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
    ]
    lib.WlanQueryInterface.restype = wintypes.DWORD
    lib.WlanFreeMemory.argtypes = [wintypes.LPVOID]
    lib.WlanFreeMemory.restype = None
    return lib


# --- helpers -------------------------------------------------------------------------


def run_hidden(
    args: list[str], timeout: float = SUBPROCESS_TIMEOUT_S
) -> subprocess.CompletedProcess[bytes]:
    """Run a console tool without flashing a window. Never raises on a non-zero exit."""
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def _decode(output: bytes) -> str:
    for encoding in ("utf-8", "mbcs"):
        try:
            return output.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return output.decode("latin-1")


def _kind(if_type: int, physical_medium: int) -> AdapterKind:
    if physical_medium == PHYSICAL_MEDIUM_BLUETOOTH:
        return "other"
    if if_type == IF_TYPE_IEEE80211:
        return "wifi"
    if if_type == IF_TYPE_ETHERNET:
        return "ethernet"
    if if_type in (IF_TYPE_WWANPP, IF_TYPE_WWANPP2):
        return "cellular"
    return "other"


def _is_noise(row: MIB_IF_ROW2) -> bool:
    """Interfaces never worth listing: NDIS filter layers (they repeat the base adapter's
    counters), loopback and IPv6 transition tunnels."""
    return bool(row.InterfaceAndOperStatusFlags & FLAG_FILTER) or row.Type in (
        IF_TYPE_LOOPBACK,
        IF_TYPE_TUNNEL,
    )


def read_if_table() -> Snapshot:
    """All interfaces via ``GetIfTable2``. Raises OSError if the call fails."""
    lib = _iphlpapi()
    table = ctypes.POINTER(MIB_IF_TABLE2)()
    rc = lib.GetIfTable2(ctypes.byref(table))
    if rc != ERROR_SUCCESS:
        raise OSError(rc, "GetIfTable2 failed")
    try:
        count = table.contents.NumEntries
        rows = ctypes.cast(
            ctypes.addressof(table.contents.Table), ctypes.POINTER(MIB_IF_ROW2 * count)
        ).contents
        adapters: dict[str, AdapterInfo] = {}
        counters: dict[str, Counters] = {}
        for row in rows:
            if _is_noise(row):
                continue
            guid = str(row.InterfaceGuid)
            flags = row.InterfaceAndOperStatusFlags
            speed = max(row.ReceiveLinkSpeed, row.TransmitLinkSpeed)
            adapters[guid] = AdapterInfo(
                name=row.Alias,
                guid=guid,
                description=row.Description,
                kind=_kind(row.Type, row.PhysicalMediumType),
                is_hardware=bool(flags & FLAG_HARDWARE)
                and not flags & (FLAG_FILTER | FLAG_ENDPOINT),
                is_connected=row.MediaConnectState == MEDIA_CONNECT_STATE_CONNECTED
                and row.OperStatus == IF_OPER_STATUS_UP,
                # Unknown speeds are reported as 0 or all-ones; both mean "no cap info".
                speed_bps=speed if 0 < speed < 2**63 else None,
            )
            counters[guid] = Counters(int(row.InOctets), int(row.OutOctets))
        return Snapshot(adapters=adapters, counters=counters)
    finally:
        lib.FreeMibTable(table)


_PSUTIL_WIFI = ("wi-fi", "wifi", "wlan", "wireless", "802.11")
_PSUTIL_CELL = ("cellular", "mobile broadband", "wwan")
_PSUTIL_VIRTUAL = ("loopback", "pseudo", "virtual", "vethernet", "vpn", "bluetooth", "tunnel")


def read_psutil() -> Snapshot:
    """Counters keyed by friendly name; kind and hardware-ness guessed from that name.

    Not usable as a snapshot on its own: the sampler keys adapters by InterfaceGuid, so
    the platform passes this through :func:`remap_to_guids` first.
    """
    io = psutil.net_io_counters(pernic=True, nowrap=False)
    stats = psutil.net_if_stats()
    adapters: dict[str, AdapterInfo] = {}
    counters: dict[str, Counters] = {}
    for name, c in io.items():
        lower = name.casefold()
        kind: AdapterKind = (
            "wifi"
            if any(k in lower for k in _PSUTIL_WIFI)
            else "cellular"
            if any(k in lower for k in _PSUTIL_CELL)
            else "ethernet"
            if "ethernet" in lower
            else "other"
        )
        st = stats.get(name)
        adapters[name] = AdapterInfo(
            name=name,
            guid=name,
            description="",
            kind=kind,
            is_hardware=not any(k in lower for k in _PSUTIL_VIRTUAL),
            is_connected=bool(st and st.isup),
            speed_bps=st.speed * 1_000_000 if st and st.speed else None,
        )
        counters[name] = Counters(int(c.bytes_recv), int(c.bytes_sent))
    return Snapshot(adapters=adapters, counters=counters)


def alias_to_guid(alias: str) -> str | None:
    """InterfaceGuid of the interface with this friendly name, or None."""
    try:
        lib = _iphlpapi()
        luid = ctypes.c_uint64()
        if lib.ConvertInterfaceAliasToLuid(alias, ctypes.byref(luid)) != ERROR_SUCCESS:
            return None
        guid = GUID()
        if lib.ConvertInterfaceLuidToGuid(ctypes.byref(luid), ctypes.byref(guid)) != ERROR_SUCCESS:
            return None
        return str(guid)
    except Exception:
        return None


def remap_to_guids(
    snap: Snapshot,
    known: Mapping[str, AdapterInfo],
    resolve: Callable[[str], str | None] = alias_to_guid,
) -> Snapshot:
    """Re-key a name-keyed :func:`read_psutil` snapshot by InterfaceGuid.

    Adapter identity must not change when the native read fails, or the sampler would
    see brand-new adapters (new rows, new baselines) and the GUID baselines would later
    count the same traffic again. ``known`` (alias -> info from the last native read)
    supplies the identity and the metadata psutil lacks; otherwise ``resolve`` looks
    the GUID up. Names that cannot be mapped are left out rather than invented.
    """
    adapters: dict[str, AdapterInfo] = {}
    counters: dict[str, Counters] = {}
    for name, guessed in snap.adapters.items():
        prior = known.get(name)
        if prior is not None:
            info = replace(
                prior,
                is_connected=guessed.is_connected,
                speed_bps=guessed.speed_bps or prior.speed_bps,
            )
        else:
            guid = resolve(name)
            if guid is None:
                continue
            info = replace(guessed, guid=guid)
        if info.guid in adapters:
            continue
        adapters[info.guid] = info
        counters[info.guid] = snap.counters[name]
    return Snapshot(adapters=adapters, counters=counters)


# --- WLAN ----------------------------------------------------------------------------


class WlanAccessDenied(OSError):
    """WlanQueryInterface refused: Location access is off for this app (Win 11 24H2+)."""


class _Wlan:
    def __init__(self) -> None:
        self._handle: wintypes.HANDLE | None = None
        self._lock = threading.Lock()

    def _open(self) -> wintypes.HANDLE:
        if self._handle is None:
            handle = wintypes.HANDLE()
            version = wintypes.DWORD()
            rc = _wlanapi().WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle))
            if rc != ERROR_SUCCESS:
                raise OSError(rc, "WlanOpenHandle failed")
            self._handle = handle
        return self._handle

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                _wlanapi().WlanCloseHandle(self._handle, None)
                self._handle = None

    def ssids(self) -> dict[str, bytes | None]:
        """Raw SSID per WLAN interface GUID (None if that interface is not connected)."""
        with self._lock:
            lib = _wlanapi()
            handle = self._open()
            ifaces = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
            rc = lib.WlanEnumInterfaces(handle, None, ctypes.byref(ifaces))
            if rc != ERROR_SUCCESS:
                self._handle = None  # e.g. WLAN service restarted; reopen next time
                raise OSError(rc, "WlanEnumInterfaces failed")
            result: dict[str, bytes | None] = {}
            denied = False
            try:
                count = ifaces.contents.dwNumberOfItems
                infos = ctypes.cast(
                    ctypes.addressof(ifaces.contents.InterfaceInfo),
                    ctypes.POINTER(WLAN_INTERFACE_INFO * count),
                ).contents
                for info in infos:
                    guid = str(info.InterfaceGuid)
                    if info.isState != WLAN_INTERFACE_STATE_CONNECTED:
                        result[guid] = None
                        continue
                    size = wintypes.DWORD()
                    data = wintypes.LPVOID()
                    rc = lib.WlanQueryInterface(
                        handle,
                        ctypes.byref(info.InterfaceGuid),
                        WLAN_INTF_OPCODE_CURRENT_CONNECTION,
                        None,
                        ctypes.byref(size),
                        ctypes.byref(data),
                        None,
                    )
                    if rc == ERROR_ACCESS_DENIED:
                        denied = True
                        continue
                    if rc != ERROR_SUCCESS:
                        result[guid] = None
                        continue
                    try:
                        attrs = ctypes.cast(data, ctypes.POINTER(WLAN_CONNECTION_ATTRIBUTES))
                        ssid = attrs.contents.wlanAssociationAttributes.dot11Ssid
                        length = min(int(ssid.uSSIDLength), 32)
                        result[guid] = bytes(ssid.ucSSID[:length]) or None
                    finally:
                        lib.WlanFreeMemory(data)
            finally:
                lib.WlanFreeMemory(ifaces)
            if denied:  # a per-app permission, so it applies to every interface
                raise WlanAccessDenied(ERROR_ACCESS_DENIED, "WLAN access denied (Location)")
            return result


def profile_names() -> dict[str, str]:
    """Connected network profile name per interface alias (Network List Manager).

    Not location-gated, but slow (~2 s, PowerShell), so callers cache it. The name is
    usually the SSID, sometimes with a " 2" suffix.
    """
    proc = run_hidden(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            'Get-NetConnectionProfile | ForEach-Object { $_.InterfaceAlias + "`t" + $_.Name }',
        ]
    )
    names: dict[str, str] = {}
    if proc.returncode != 0:
        return names
    for line in _decode(proc.stdout).splitlines():
        alias, sep, name = line.partition("\t")
        if sep and alias.strip() and name.strip():
            names[alias.strip()] = name.strip()
    return names


# --- identity ------------------------------------------------------------------------


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


def _sid_via_ctypes() -> str:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel = _kernel32()
    token = wintypes.HANDLE()
    TOKEN_QUERY = 0x0008
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(buf, ctypes.POINTER(_TOKEN_USER)).contents
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(user.Sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(text.value)
        finally:
            kernel.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
    finally:
        kernel.CloseHandle(token)


@functools.cache
def user_sid() -> str:
    """Current user's SID (``S-1-5-21-...``); falls back to the user name."""
    try:
        return _sid_via_ctypes()
    except Exception as exc:
        log.warning("SID via ctypes failed (%s); trying whoami", exc)
    try:
        proc = run_hidden(["whoami", "/user", "/fo", "csv", "/nh"], timeout=10)
        sid = _decode(proc.stdout).strip().rsplit(",", 1)[-1].strip().strip('"')
        if sid.startswith("S-"):
            return sid
    except Exception as exc:
        log.warning("whoami failed: %s", exc)
    return getpass.getuser()


def _user_id() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


# --- kernel objects ------------------------------------------------------------------


class WindowsMutexLock:
    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    def release(self) -> None:
        if self._handle is not None:
            kernel = _kernel32()
            kernel.ReleaseMutex(self._handle)
            kernel.CloseHandle(self._handle)
            self._handle = None


class WindowsStopSignal:
    """Named auto-reset event. Waits in short slices so Ctrl+C still works in a console."""

    def __init__(self, name: str) -> None:
        self._handle: int | None = _kernel32().CreateEventW(None, False, False, name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout_s: float) -> bool:
        if self._handle is None:
            return False
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            slice_ms = int(max(0.0, min(1.0, deadline - time.monotonic())) * 1000)
            if _kernel32().WaitForSingleObject(self._handle, slice_ms) == WAIT_OBJECT_0:
                return True
            if time.monotonic() >= deadline:
                return False

    def close(self) -> None:
        if self._handle is not None:
            _kernel32().CloseHandle(self._handle)
            self._handle = None


class _ThreadStopSignal:
    """In-process fallback if the named event cannot be created."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def wait(self, timeout_s: float) -> bool:
        if self._event.wait(timeout_s):
            self._event.clear()
            return True
        return False

    def close(self) -> None:
        pass


# --- the platform --------------------------------------------------------------------


class WindowsPlatform:
    # The native WLAN query is a fast in-process RPC; the slow profile-name fallback
    # (PowerShell) is cached here, per adapter, for PROFILE_CACHE_S.
    ssid_refresh_s = 0

    def __init__(self, object_prefix: str = "ByteLog.Collector") -> None:
        """``object_prefix`` names the kernel objects; tests use a different one."""
        sid = user_sid()
        self.mutex_name = f"Global\\{object_prefix}.{sid}"
        self.stop_event_name = f"Global\\{object_prefix}.Stop.{sid}"
        self._wlan = _Wlan()
        self._native_failed_at: float | None = None  # monotonic time of the last failure
        self._known: dict[str, AdapterInfo] = {}  # alias -> info from the last native read
        self._local_stop = threading.Event()
        self._last_snapshot_mono: float | None = None
        self._connected: dict[str, bool] = {}
        self._profile_cache: dict[str, tuple[bytes | None, float]] = {}
        self._toaster: Any = None
        self._wlan_problem: str | None = None  # last logged WLAN failure, to log changes only

    # snapshot -------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        snap = self._read_native()
        if snap is None:
            snap = remap_to_guids(read_psutil(), self._known)
        self._track_changes(snap)
        return snap

    def _read_native(self) -> Snapshot | None:
        """``GetIfTable2``, retried every ``NATIVE_RETRY_S`` after a failure."""
        failed_at = self._native_failed_at
        if failed_at is not None and time.monotonic() - failed_at < NATIVE_RETRY_S:
            return None
        try:
            snap = read_if_table()
        except Exception:
            if failed_at is None:
                log.exception("GetIfTable2 failed; falling back to psutil")
            self._native_failed_at = time.monotonic()
            return None
        if failed_at is not None:
            log.info("GetIfTable2 works again")
        self._native_failed_at = None
        self._known = {info.name: info for info in snap.adapters.values()}
        return snap

    def _track_changes(self, snap: Snapshot) -> None:
        """Invalidate cached profile names on reconnects and after sleep."""
        mono = time.monotonic()
        if self._last_snapshot_mono is not None and mono - self._last_snapshot_mono > 60:
            self._profile_cache.clear()
        self._last_snapshot_mono = mono
        for guid, info in snap.adapters.items():
            if self._connected.get(guid) != info.is_connected:
                self._profile_cache.pop(guid, None)
            self._connected[guid] = info.is_connected

    # SSID -----------------------------------------------------------------------------

    def wifi_ssid(self, adapter: AdapterInfo) -> bytes | None:
        try:
            ssids = self._wlan.ssids()
        except WlanAccessDenied:
            self._wlan_failed(
                logging.INFO, "WLAN API access denied (Location off); using network profile names"
            )
        except Exception as exc:
            self._wlan_failed(logging.WARNING, f"WLAN query failed: {exc}")
        else:
            if self._wlan_problem is not None:
                log.info("WLAN API works again")
                self._wlan_problem = None
            if adapter.guid in ssids:
                return ssids[adapter.guid]
            log.debug("WLAN API does not list %s; using the network profile name", adapter.name)
        return self._profile_ssid(adapter)

    def _wlan_failed(self, level: int, message: str) -> None:
        """Called on every sample while the WLAN API fails, so only changes are logged."""
        if message != self._wlan_problem:
            log.log(level, "%s", message)
            self._wlan_problem = message

    def _profile_ssid(self, adapter: AdapterInfo) -> bytes | None:
        cached = self._profile_cache.get(adapter.guid)
        if cached is not None and time.monotonic() - cached[1] < PROFILE_CACHE_S:
            return cached[0]
        name: str | None = None
        try:
            name = profile_names().get(adapter.name)
        except Exception as exc:
            log.warning("Get-NetConnectionProfile failed: %s", exc)
        value = name.encode("utf-8") if name else None
        self._profile_cache[adapter.guid] = (value, time.monotonic())
        return value

    # misc -----------------------------------------------------------------------------

    def boot_time_utc(self) -> int:
        return int(psutil.boot_time())

    def acquire_collector_lock(self) -> WindowsMutexLock | None:
        kernel = _kernel32()
        handle = kernel.CreateMutexW(None, True, self.mutex_name)
        error = ctypes.get_last_error()
        if not handle:
            if error == ERROR_ACCESS_DENIED:
                return None  # exists, owned by another (elevated) process of this user
            raise ctypes.WinError(error)
        if error == ERROR_ALREADY_EXISTS:
            kernel.CloseHandle(handle)
            return None
        return WindowsMutexLock(handle)

    def collector_lock_held(self) -> bool:
        kernel = _kernel32()
        handle = kernel.OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, False, self.mutex_name)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
                kernel.ReleaseMutex(handle)  # nobody owned it (a crashed collector)
                return False
            return True
        finally:
            kernel.CloseHandle(handle)

    def open_stop_signal(self) -> WindowsStopSignal | _ThreadStopSignal:
        try:
            return WindowsStopSignal(self.stop_event_name)
        except Exception:
            log.exception("could not create the stop event; using an in-process signal")
            return _ThreadStopSignal(self._local_stop)

    def signal_stop(self) -> None:
        self._local_stop.set()
        try:
            kernel = _kernel32()
            handle = kernel.CreateEventW(None, False, False, self.stop_event_name)
            if handle:
                kernel.SetEvent(handle)
                kernel.CloseHandle(handle)
        except Exception:
            log.exception("could not signal the stop event")

    # notifications ----------------------------------------------------------------------

    def notify(self, title: str, message: str) -> bool:
        try:
            if self._toaster is None:
                from windows_toasts import InteractableWindowsToaster

                register_aumid()
                self._toaster = InteractableWindowsToaster("ByteLog", notifierAUMID=AUMID)
            from windows_toasts import Toast

            self._toaster.show_toast(Toast(text_fields=[title, message]))
            return True
        except Exception as exc:
            log.warning("toast failed: %s", exc)
            return False

    # processes ----------------------------------------------------------------------------

    def launch_collector(self) -> None:
        command = collector_command()
        env = dict(os.environ)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "env": env,
            "cwd": str(Path(command[0]).parent),
        }
        try:
            subprocess.Popen(command, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            # The parent's job object may not allow breakaway; plain detach is enough then.
            subprocess.Popen(command, creationflags=flags, **kwargs)
        log.info("launched collector: %s", subprocess.list2cmdline(command))

    def autostart_enabled(self) -> bool:
        try:
            if run_hidden(["schtasks", "/query", "/tn", TASK_NAME]).returncode == 0:
                return True
        except Exception as exc:
            log.warning("schtasks /query failed: %s", exc)
        return _run_value() is not None

    def autostart_is_current(self) -> bool:
        registered = registered_autostart_exe()
        if registered is None:
            return False
        return _same_path(registered, collector_command()[0])

    def set_autostart(self, enabled: bool) -> None:
        if not enabled:
            try:
                run_hidden(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
            except Exception as exc:
                log.warning("schtasks /delete failed: %s", exc)
            _set_run_value(None)
            return
        if _create_task():
            _set_run_value(None)
        else:
            log.warning("logon task could not be created; using the HKCU Run key")
            _set_run_value(subprocess.list2cmdline(collector_command()))

    def open_path(self, path: Path) -> None:
        os.startfile(str(path))  # type: ignore[attr-defined]


# --- launching and autostart ---------------------------------------------------------


def collector_command() -> list[str]:
    """Command line that starts the collector without a console (dev and frozen)."""
    if is_frozen():
        command = [str(Path(sys.executable).with_name(COLLECTOR_EXE))]
    else:
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        command = [str(pythonw if pythonw.exists() else exe), "-m", "bytelog.collector"]
    home = os.environ.get(ENV_HOME)
    if home:
        command += ["--data-dir", home]
    return command


def build_task_xml(command: list[str], user_id: str | None = None) -> str:
    """Task Scheduler XML: per-user logon trigger, no battery or time limits."""
    user = escape(user_id or _user_id())
    exe = escape(command[0])
    args = subprocess.list2cmdline(command[1:])
    arguments = f"\n      <Arguments>{escape(args)}</Arguments>" if args else ""
    workdir = escape(str(Path(command[0]).parent))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>ByteLog</Author>
    <Description>Records internet data usage for ByteLog.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>{arguments}
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _create_task(task_name: str = TASK_NAME) -> bool:
    xml = build_task_xml(collector_command())
    fd, name = tempfile.mkstemp(prefix="bytelog-task-", suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(xml.encode("utf-16"))  # little-endian with BOM on Windows
        proc = run_hidden(["schtasks", "/create", "/tn", task_name, "/xml", name, "/f"])
        if proc.returncode != 0:
            log.warning("schtasks /create failed (%d): %s", proc.returncode, _decode(proc.stderr))
        return proc.returncode == 0
    except Exception as exc:
        log.warning("schtasks /create failed: %s", exc)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def registered_autostart_exe() -> str | None:
    """Executable that start-at-login runs: the logon task's command, else the Run value."""
    try:
        proc = run_hidden(["schtasks", "/query", "/tn", TASK_NAME, "/xml"])
        if proc.returncode == 0:
            command = task_xml_command(proc.stdout)
            if command:
                return command
    except Exception as exc:
        log.warning("schtasks /query /xml failed: %s", exc)
    value = _run_value()
    return first_argument(value) if value else None


def task_xml_command(output: bytes) -> str | None:
    """``<Command>`` of a ``schtasks /query /xml`` dump (UTF-16 or ANSI, per Windows build)."""
    text = output.decode("utf-16") if output[:2] in (b"\xff\xfe", b"\xfe\xff") else _decode(output)
    match = re.search(r"<Command>(.*?)</Command>", text, re.DOTALL)
    return html.unescape(match.group(1).strip()) if match else None


def first_argument(command_line: str) -> str:
    """First token of a Windows command line (the executable)."""
    command_line = command_line.strip()
    if command_line.startswith('"'):
        end = command_line.find('"', 1)
        return command_line[1:end] if end > 0 else command_line[1:]
    return command_line.split(" ", 1)[0]


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _run_value() -> str | None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return str(value)
    except OSError:
        return None


def _set_run_value(command: str | None) -> None:
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if command is None:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, RUN_VALUE)
            else:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    except OSError as exc:
        log.warning("could not update the Run key: %s", exc)


def register_aumid() -> None:
    """Register ``ByteLog.App`` so toasts show ByteLog's name and icon (no admin needed)."""
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{AUMID}"
    ) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "ByteLog")
        icon = resource_path("icon.png")
        if icon.exists():
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
