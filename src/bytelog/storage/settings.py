"""Typed access to the ``settings`` key/value table.

Settings double as the control channel from the dashboard to the collector:
``collection_paused``, ``stop_requested`` and ``reset_requested`` are written by the
dashboard and acted on by the collector on its next tick.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from bytelog.storage.db import write_tx

# Case-insensitive substrings matched against adapter name and description. This is a
# safety net on top of the OS "hardware interface" flag; see collector/filter.py.
DEFAULT_EXCLUDE_PATTERNS: list[str] = [
    "loopback",
    "vpn",
    "virtual",
    "hyper-v",
    "vethernet",
    "wsl",
    "docker",
    "virtualbox",
    "vmware",
    "bluetooth",
    "tap-",
    "tap adapter",
    "wintun",
    "wireguard",
    "openvpn",
    "tailscale",
    "zerotier",
    "hamachi",
    "anyconnect",
    "fortinet",
    "pangp",
    "teredo",
    "isatap",
    "6to4",
    "ip-https",
    "wan miniport",
    "wi-fi direct",
    "kernel debug",
    "pseudo",
    "npcap",
]

DEFAULTS: dict[str, str] = {
    "sample_interval_s": "10",  # clamped to 5..60
    "week_start": "0",  # 0 = Monday ... 6 = Sunday
    "theme": "light",  # light | dark
    "start_at_login": "1",
    "collection_paused": "0",
    "stop_requested": "",  # token; collector clears it when it exits
    "reset_requested": "",  # token; collector echoes it into reset_done
    "reset_done": "",
    "reset_forget_networks": "0",
    "exclude_patterns": json.dumps(DEFAULT_EXCLUDE_PATTERNS),
    "default_alert_thresholds": "80,100",
}

MIN_INTERVAL_S = 5
MAX_INTERVAL_S = 60


def get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        return str(row[0])
    return DEFAULTS.get(key, "")


def get_int(conn: sqlite3.Connection, key: str) -> int:
    try:
        return int(get(conn, key))
    except ValueError:
        return int(DEFAULTS.get(key, "0") or 0)


def get_bool(conn: sqlite3.Connection, key: str) -> bool:
    return get(conn, key).strip().lower() in {"1", "true", "yes", "on"}


def get_json(conn: sqlite3.Connection, key: str) -> Any:
    try:
        return json.loads(get(conn, key))
    except json.JSONDecodeError:
        return json.loads(DEFAULTS[key]) if key in DEFAULTS else None


def get_all(conn: sqlite3.Connection) -> dict[str, str]:
    values = dict(DEFAULTS)
    values.update({row[0]: row[1] for row in conn.execute("SELECT key, value FROM settings")})
    return values


def sample_interval_s(conn: sqlite3.Connection) -> int:
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, get_int(conn, "sample_interval_s")))


def week_start(conn: sqlite3.Connection) -> int:
    return get_int(conn, "week_start") % 7


def to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Write one setting in its own transaction."""
    with write_tx(conn):
        set_value_in_tx(conn, key, value)


def set_value_in_tx(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Write one setting inside a transaction the caller already holds."""
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, to_text(value)),
    )
