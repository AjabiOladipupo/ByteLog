"""Adapters, networks and collector status.

``upsert_*`` and ``write_heartbeat`` are single statements meant to run inside the
caller's transaction (the collector's per-tick ``write_tx``). The dashboard-facing
writers (``update_network``, ``set_adapter_override``) open their own transaction unless
one is already active.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import EllipsisType

from bytelog.platform.base import AdapterInfo
from bytelog.storage.db import write_tx
from bytelog.storage.models import Adapter, CollectorStatus, Network

NETWORK_KINDS = frozenset({"wifi", "ethernet", "cellular", "wifi_unknown"})


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
    else:
        with write_tx(conn):
            yield conn


def _opt_bool(value: object) -> bool | None:
    return None if value is None else bool(value)


def upsert_adapter(conn: sqlite3.Connection, info: AdapterInfo, now_utc: int) -> int:
    """Insert or refresh an adapter by GUID (renames update ``name``). Returns its id."""
    row = conn.execute(
        """INSERT INTO adapters(guid, name, description, kind, first_seen_utc, last_seen_utc)
           VALUES(?, ?, ?, ?, ?, ?)
           ON CONFLICT(guid) DO UPDATE SET
               name=excluded.name,
               description=excluded.description,
               kind=excluded.kind,
               last_seen_utc=excluded.last_seen_utc
           RETURNING id""",
        (info.guid, info.name, info.description, info.kind, now_utc, now_utc),
    ).fetchone()
    return int(row[0])


def upsert_network(
    conn: sqlite3.Connection,
    kind: str,
    identity: str,
    ssid: str | None,
    adapter_name: str | None,
    now_utc: int,
) -> int:
    """Insert or refresh a network by ``(kind, identity)``. Returns its id.

    ``ssid``/``adapter_name`` are display values and are updated when given (an Ethernet
    adapter rename shows up here); ``None`` keeps the stored value.
    """
    if kind not in NETWORK_KINDS:
        raise ValueError(f"unknown network kind {kind!r}")
    row = conn.execute(
        """INSERT INTO networks(kind, identity, ssid, adapter_name, first_seen_utc,
                                last_seen_utc)
           VALUES(?, ?, ?, ?, ?, ?)
           ON CONFLICT(kind, identity) DO UPDATE SET
               ssid=COALESCE(excluded.ssid, ssid),
               adapter_name=COALESCE(excluded.adapter_name, adapter_name),
               last_seen_utc=excluded.last_seen_utc
           RETURNING id""",
        (kind, identity, ssid, adapter_name, now_utc, now_utc),
    ).fetchone()
    return int(row[0])


def _network(row: sqlite3.Row) -> Network:
    return Network(
        id=row["id"],
        kind=row["kind"],
        identity=row["identity"],
        ssid=row["ssid"],
        adapter_name=row["adapter_name"],
        user_label=row["user_label"],
        is_metered=bool(row["is_metered"]),
        first_seen_utc=row["first_seen_utc"],
        last_seen_utc=row["last_seen_utc"],
    )


def list_networks(conn: sqlite3.Connection) -> list[Network]:
    rows = conn.execute("SELECT * FROM networks ORDER BY last_seen_utc DESC, id").fetchall()
    return [_network(row) for row in rows]


def get_network(conn: sqlite3.Connection, network_id: int) -> Network | None:
    row = conn.execute("SELECT * FROM networks WHERE id=?", (network_id,)).fetchone()
    return _network(row) if row else None


def update_network(
    conn: sqlite3.Connection,
    network_id: int,
    *,
    user_label: str | None | EllipsisType = ...,
    is_metered: bool | None = None,
) -> None:
    """Change a network's label and/or metered flag.

    ``user_label=...`` (default) leaves the label alone; ``None`` or ``""`` clears it.
    ``is_metered=None`` leaves the flag alone.
    """
    sets: list[str] = []
    params: list[object] = []
    if not isinstance(user_label, EllipsisType):
        label = user_label.strip() if user_label else ""
        sets.append("user_label=?")
        params.append(label or None)
    if is_metered is not None:
        sets.append("is_metered=?")
        params.append(1 if is_metered else 0)
    if not sets:
        return
    with _tx(conn):
        conn.execute(f"UPDATE networks SET {', '.join(sets)} WHERE id=?", (*params, network_id))


def _adapter(row: sqlite3.Row) -> Adapter:
    return Adapter(
        id=row["id"],
        guid=row["guid"],
        name=row["name"],
        description=row["description"],
        kind=row["kind"],
        include_override=_opt_bool(row["include_override"]),
        first_seen_utc=row["first_seen_utc"],
        last_seen_utc=row["last_seen_utc"],
    )


def list_adapters(conn: sqlite3.Connection) -> list[Adapter]:
    rows = conn.execute("SELECT * FROM adapters ORDER BY last_seen_utc DESC, name").fetchall()
    return [_adapter(row) for row in rows]


def adapter_overrides(conn: sqlite3.Connection) -> dict[str, bool | None]:
    """``include_override`` keyed by adapter GUID (read by the collector every tick)."""
    return {
        row[0]: _opt_bool(row[1])
        for row in conn.execute("SELECT guid, include_override FROM adapters")
    }


def set_adapter_override(conn: sqlite3.Connection, adapter_id: int, include: bool | None) -> None:
    """``True`` always count, ``False`` never count, ``None`` follow the default rule."""
    value = None if include is None else int(include)
    with _tx(conn):
        conn.execute("UPDATE adapters SET include_override=? WHERE id=?", (value, adapter_id))


def read_collector_status(conn: sqlite3.Connection) -> CollectorStatus:
    row = conn.execute(
        "SELECT pid, state, started_utc, heartbeat_utc, beat, app_version, last_error "
        "FROM collector_status WHERE id=1"
    ).fetchone()
    if row is None:
        return CollectorStatus(None, "stopped", None, None, 0, None, None)
    return CollectorStatus(
        pid=row["pid"],
        state=row["state"],
        started_utc=row["started_utc"],
        heartbeat_utc=row["heartbeat_utc"],
        beat=row["beat"],
        app_version=row["app_version"],
        last_error=row["last_error"],
    )


def write_heartbeat(
    conn: sqlite3.Connection,
    *,
    pid: int,
    state: str,
    version: str,
    last_error: str | None = None,
    now_utc: int | None = None,
    started_utc: int | None = None,
) -> None:
    """Bump the heartbeat (caller holds the transaction).

    ``started_utc`` is written when given and kept otherwise; ``last_error`` is always
    written, so a clean tick clears an earlier error.
    """
    now = int(time.time()) if now_utc is None else now_utc
    conn.execute(
        """UPDATE collector_status SET
               pid=?, state=?, app_version=?, last_error=?, heartbeat_utc=?,
               beat=beat + 1, started_utc=COALESCE(?, started_utc)
           WHERE id=1""",
        (pid, state, version, last_error, now, started_utc),
    )
