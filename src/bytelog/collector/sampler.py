"""One collector tick: read counters, compute deltas, write samples + baselines + heartbeat.

Everything a tick writes goes into one ``BEGIN IMMEDIATE`` transaction, so a crash can
neither double count nor lose bytes: if the commit fails, the in-memory baselines are
rolled back too and the next tick's delta includes the missed bytes.

Baselines are kept for every adapter the OS reports, counted or not, so including an
adapter later starts from a fresh baseline instead of backfilling its whole counter.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import tzinfo

from bytelog import __version__, timeutil
from bytelog.collector.delta import (
    FLAG_CAPPED,
    FLAG_CLOCK,
    FLAG_GAP,
    FLAG_RESET,
    FLAG_SSID_UNKNOWN,
    Baseline,
    DeltaResult,
    compute_delta,
)
from bytelog.collector.filter import should_count
from bytelog.platform.base import AdapterInfo, Counters, Platform
from bytelog.storage import repo, settings
from bytelog.storage.db import write_tx

log = logging.getLogger(__name__)

SSID_RETRY_S = 60  # re-query a failed lookup at least this often
LAST_NETWORK_GRACE_S = 300  # reuse the last known Wi-Fi network this long when lookups fail
ROW_REFRESH_S = 60  # bump last_seen_utc of unchanged adapter/network rows at most this often


@dataclass(frozen=True)
class _SsidEntry:
    ssid: bytes | None
    fetched_utc: int


@dataclass(frozen=True)
class _RowCache:
    id: int
    fingerprint: tuple[object, ...]
    written_utc: int


@dataclass(frozen=True)
class _Reading:
    info: AdapterInfo
    cur: Counters
    delta: DeltaResult
    counted: bool


@dataclass
class _Caches:
    """State that must roll back together with a failed tick transaction."""

    baselines: dict[str, Baseline] = field(default_factory=dict)
    baseline_mono: dict[str, float] = field(default_factory=dict)  # baselines from this run
    last_network: dict[str, tuple[int, int]] = field(default_factory=dict)  # (id, seen_utc)
    adapter_rows: dict[str, _RowCache] = field(default_factory=dict)
    network_rows: dict[tuple[str, str], _RowCache] = field(default_factory=dict)

    def copy(self) -> _Caches:
        return _Caches(
            dict(self.baselines),
            dict(self.baseline_mono),
            dict(self.last_network),
            dict(self.adapter_rows),
            dict(self.network_rows),
        )


def _fresh(written_utc: int, now_utc: int, max_age_s: int) -> bool:
    return 0 <= now_utc - written_utc < max_age_s


class Sampler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        platform: Platform,
        tz_provider: Callable[[], tzinfo],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        pid: int | None = None,
        version: str = __version__,
    ) -> None:
        self.conn = conn
        self.platform = platform
        self.tz_provider = tz_provider
        self._monotonic = monotonic
        self.pid = os.getpid() if pid is None else pid
        self.version = version
        self._started_utc: int | None = None
        self.last_error: str | None = None  # set by the service; written with the heartbeat

        self._loaded = False
        self._c = _Caches()
        self._ssid_cache: dict[str, _SsidEntry] = {}
        self._connected: dict[str, bool] = {}

    # --- public ------------------------------------------------------------------------

    def tick(self, now_utc: int | None = None, paused: bool = False) -> int:
        """Sample once. Returns the number of sample rows written."""
        now = timeutil.now_utc_s() if now_utc is None else now_utc
        mono = self._monotonic()
        if self._started_utc is None:
            self._started_utc = now
        if not self._loaded:
            self._load_state()

        snap = self.platform.snapshot()
        boot = self.platform.boot_time_utc()
        interval = settings.sample_interval_s(self.conn)
        patterns = self._patterns()
        overrides = repo.adapter_overrides(self.conn)
        offset = timeutil.utc_offset_s(now, self.tz_provider())

        readings: list[_Reading] = []
        for guid, info in snap.adapters.items():
            cur = snap.counters.get(guid)
            if cur is None:
                continue
            if self._connected.get(guid) != info.is_connected:
                self._ssid_cache.pop(guid, None)  # connection changed: SSID may differ
            self._connected[guid] = info.is_connected
            mono_elapsed = (
                mono - self._c.baseline_mono[guid] if guid in self._c.baseline_mono else None
            )
            delta = compute_delta(
                self._c.baselines.get(guid), cur, now, boot, interval, info.speed_bps, mono_elapsed
            )
            counted = should_count(info, patterns, overrides.get(guid))
            readings.append(_Reading(info, cur, delta, counted))
            if counted:
                self._log_delta(info, delta)

        # Slow, possibly permission-gated SSID lookups happen before taking the write lock.
        if not paused:
            for r in readings:
                if self._needs_sample(r) and r.info.kind == "wifi" and r.info.is_connected:
                    self._refresh_ssid(r.info, r.delta, now)

        saved = self._c.copy()
        try:
            return self._write(readings, now, mono, boot, offset, paused)
        except BaseException:
            self._c = saved
            raise

    def reset_caches(self) -> None:
        """Forget cached row ids (after a reset that may have deleted adapters/networks).

        In-memory baselines are kept, so traffic since the last tick is still counted.
        """
        self._c.adapter_rows.clear()
        self._c.network_rows.clear()
        self._c.last_network.clear()

    # --- tick internals ----------------------------------------------------------------

    def _write(
        self,
        readings: list[_Reading],
        now: int,
        mono: float,
        boot: int,
        offset: int,
        paused: bool,
    ) -> int:
        rows = 0
        with write_tx(self.conn):
            for r in readings:
                guid = r.info.guid
                adapter_id = self._ensure_adapter(r.info, now)
                last = self._c.last_network.get(guid)
                network_id = last[0] if last else None
                if not paused and self._needs_sample(r):
                    network_id, extra_flags = self._resolve_network(r.info, now)
                    self.conn.execute(
                        """INSERT INTO samples(ts_utc, span_s, utc_offset_s, network_id,
                                               adapter_id, rx_bytes, tx_bytes, flags)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            now,
                            r.delta.span_s,
                            offset,
                            network_id,
                            adapter_id,
                            r.delta.rx,
                            r.delta.tx,
                            r.delta.flags | extra_flags,
                        ),
                    )
                    rows += 1
                self.conn.execute(
                    """INSERT INTO collector_state(adapter_id, last_rx, last_tx, last_ts_utc,
                                                   last_network_id, boot_time_utc)
                       VALUES(?, ?, ?, ?, ?, ?)
                       ON CONFLICT(adapter_id) DO UPDATE SET
                           last_rx=excluded.last_rx, last_tx=excluded.last_tx,
                           last_ts_utc=excluded.last_ts_utc,
                           last_network_id=COALESCE(excluded.last_network_id, last_network_id),
                           boot_time_utc=excluded.boot_time_utc""",
                    (adapter_id, r.cur.rx, r.cur.tx, now, network_id, boot),
                )
                self._c.baselines[guid] = Baseline(r.cur.rx, r.cur.tx, now, boot)
                self._c.baseline_mono[guid] = mono
            repo.write_heartbeat(
                self.conn,
                pid=self.pid,
                state="paused" if paused else "running",
                version=self.version,
                last_error=self.last_error,
                now_utc=now,
                started_utc=self._started_utc,
            )
        return rows

    @staticmethod
    def _needs_sample(r: _Reading) -> bool:
        return r.counted and r.delta.total > 0

    def _resolve_network(self, info: AdapterInfo, now: int) -> tuple[int, int]:
        """Network the adapter's bytes belong to, plus extra sample flags."""
        guid = info.guid
        if info.kind != "wifi":
            kind = info.kind if info.kind in ("ethernet", "cellular") else "ethernet"
            network_id = self._ensure_network(kind, guid, None, info.name, now)
            self._c.last_network[guid] = (network_id, now)
            return network_id, 0

        entry = self._ssid_cache.get(guid) if info.is_connected else None
        if entry is not None and entry.ssid:
            ssid_text = entry.ssid.decode("utf-8", errors="replace")
            network_id = self._ensure_network(
                "wifi", "ssid:" + entry.ssid.hex(), ssid_text, info.name, now
            )
            last = self._c.last_network.get(guid)
            if last is not None and last[0] != network_id:
                log.info("adapter %s now on Wi-Fi network %r", info.name, ssid_text)
            self._c.last_network[guid] = (network_id, now)
            return network_id, 0

        # SSID unknown (lookup failed, Location permission off, or the adapter just
        # disconnected): keep attributing to the last known network for a while.
        last = self._c.last_network.get(guid)
        if last is not None and (
            not info.is_connected or 0 <= now - last[1] <= LAST_NETWORK_GRACE_S
        ):
            return last[0], 0
        network_id = self._ensure_network("wifi_unknown", guid, None, info.name, now)
        return network_id, FLAG_SSID_UNKNOWN

    def _refresh_ssid(self, info: AdapterInfo, delta: DeltaResult, now: int) -> None:
        entry = self._ssid_cache.get(info.guid)
        if entry is not None and not delta.flags & FLAG_GAP:
            # A Wi-Fi switch can drop the link for less than one interval, so the cache
            # is only as long as the platform says a lookup is expensive enough to need.
            refresh = self.platform.ssid_refresh_s
            max_age = refresh if entry.ssid else min(SSID_RETRY_S, refresh)
            if _fresh(entry.fetched_utc, now, max_age):
                return
        try:
            ssid = self.platform.wifi_ssid(info)
        except Exception as exc:
            log.warning("SSID lookup failed for %s: %s", info.name, exc)
            ssid = None
        if not ssid and (entry is None or entry.ssid):
            log.info("SSID unavailable for %s", info.name)
        self._ssid_cache[info.guid] = _SsidEntry(ssid or None, now)

    def _ensure_adapter(self, info: AdapterInfo, now: int) -> int:
        fingerprint = (info.name, info.description, info.kind)
        cached = self._c.adapter_rows.get(info.guid)
        if (
            cached is not None
            and cached.fingerprint == fingerprint
            and _fresh(cached.written_utc, now, ROW_REFRESH_S)
        ):
            return cached.id
        if cached is not None and cached.fingerprint[0] != info.name:
            log.info("adapter renamed: %r -> %r", cached.fingerprint[0], info.name)
        adapter_id = repo.upsert_adapter(self.conn, info, now)
        self._c.adapter_rows[info.guid] = _RowCache(adapter_id, fingerprint, now)
        return adapter_id

    def _ensure_network(
        self, kind: str, identity: str, ssid: str | None, adapter_name: str | None, now: int
    ) -> int:
        key = (kind, identity)
        fingerprint = (ssid, adapter_name)
        cached = self._c.network_rows.get(key)
        if (
            cached is not None
            and cached.fingerprint == fingerprint
            and _fresh(cached.written_utc, now, ROW_REFRESH_S)
        ):
            return cached.id
        network_id = repo.upsert_network(self.conn, kind, identity, ssid, adapter_name, now)
        if cached is None:
            log.debug("network %s:%s -> id %d", kind, identity, network_id)
        self._c.network_rows[key] = _RowCache(network_id, fingerprint, now)
        return network_id

    def _patterns(self) -> list[str]:
        value = settings.get_json(self.conn, "exclude_patterns")
        if not isinstance(value, list):
            value = settings.DEFAULT_EXCLUDE_PATTERNS
        return [p for p in value if isinstance(p, str)]

    def _load_state(self) -> None:
        rows = self.conn.execute(
            """SELECT a.guid, s.last_rx, s.last_tx, s.last_ts_utc, s.last_network_id,
                      s.boot_time_utc
               FROM collector_state s JOIN adapters a ON a.id = s.adapter_id"""
        ).fetchall()
        for guid, rx, tx, ts, network_id, boot in rows:
            self._c.baselines.setdefault(guid, Baseline(rx, tx, ts, boot))
            if network_id is not None:
                self._c.last_network.setdefault(guid, (network_id, ts))
        self._loaded = True
        log.info("restored %d adapter baselines", len(rows))

    def _log_delta(self, info: AdapterInfo, delta: DeltaResult) -> None:
        if delta.flags & FLAG_GAP:
            log.info(
                "gap of %d s on %s: %d B down, %d B up recorded at wake time",
                delta.span_s,
                info.name,
                delta.rx,
                delta.tx,
            )
        if delta.flags & FLAG_RESET:
            log.info(
                "counter reset on %s (%s): counting %d B since the restart",
                info.name,
                delta.rebaseline_reason,
                delta.total,
            )
        if delta.flags & FLAG_CAPPED:
            log.warning("implausible jump on %s ignored and rebaselined", info.name)
        if delta.flags & FLAG_CLOCK:
            log.warning("system clock changed (span %d s on %s)", delta.span_s, info.name)
