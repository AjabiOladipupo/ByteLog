"""The collector main loop and its control protocol (see docs/ARCHITECTURE.md).

Each iteration: read control settings -> ``Sampler.tick`` -> rollup -> alerts ->
retention (hourly) -> wait ``sample_interval_s`` on the stop signal. Every step is
isolated: an exception is logged and stored as ``last_error``, and the loop carries on.
"""

from __future__ import annotations

import faulthandler
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import tzinfo
from types import TracebackType
from typing import IO, Any

from bytelog import timeutil
from bytelog.collector.sampler import Sampler
from bytelog.platform.base import InstanceLock, Platform
from bytelog.storage import maintenance, repo, settings
from bytelog.storage.db import write_tx

log = logging.getLogger(__name__)

TZ_REFRESH_S = 60
RETENTION_EVERY_S = 3600

_warned_missing: set[str] = set()
_fault_file: IO[str] | None = None


# --- steps owned by other modules (imported lazily so the collector runs without them) --


def _optional(step: str, module: str, attr: str) -> Callable[..., Any] | None:
    try:
        mod = __import__(module, fromlist=[attr])
        return getattr(mod, attr)
    except (ImportError, AttributeError) as exc:
        if step not in _warned_missing:
            _warned_missing.add(step)
            log.warning("%s step unavailable: %s", step, exc)
        return None


def rollup_step(conn: sqlite3.Connection) -> None:
    fn = _optional("rollup", "bytelog.storage.rollup", "run_rollup")
    if fn is not None:
        fn(conn)


def alerts_step(conn: sqlite3.Connection, platform: Platform, now_utc: int, tz: tzinfo) -> None:
    fn = _optional("alerts", "bytelog.collector.alerts", "check_alerts")
    if fn is not None:
        fn(conn, platform, now_utc, tz)


def retention_step(conn: sqlite3.Connection, now_utc: int) -> None:
    fn = _optional("retention", "bytelog.storage.retention", "run_retention")
    if fn is not None:
        deleted = fn(conn, now_utc)
        if deleted and any(deleted.values()):
            log.info("retention deleted %s", deleted)


# --- loop ----------------------------------------------------------------------------


class _Loop:
    def __init__(
        self,
        conn: sqlite3.Connection,
        platform: Platform,
        sampler: Sampler | None,
        monotonic: Callable[[], float],
    ) -> None:
        self.conn = conn
        self.platform = platform
        self.monotonic = monotonic
        self.tz = timeutil.local_zone(refresh=True)
        self.tz_refreshed = monotonic()
        self.sampler = sampler or Sampler(conn, platform, lambda: self.tz, monotonic=monotonic)
        self.last_retention: float | None = None
        self.paused: bool | None = None
        self.errors: list[str] = []

    def step(self, name: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as exc:
            log.exception("collector step %r failed", name)
            self.errors.append(f"{name}: {type(exc).__name__}: {exc}")

    def refresh_tz(self) -> None:
        if self.monotonic() - self.tz_refreshed >= TZ_REFRESH_S:
            self.tz_refreshed = self.monotonic()
            zone = timeutil.local_zone(refresh=True)
            if str(zone) != str(self.tz):
                log.info("time zone changed: %s -> %s", self.tz, zone)
            self.tz = zone

    def read_paused(self) -> bool:
        paused = settings.get_bool(self.conn, "collection_paused")
        if paused != self.paused:
            if self.paused is not None or paused:
                log.info("collection %s", "paused" if paused else "resumed")
            self.paused = paused
        return paused

    def handle_reset(self) -> None:
        token = settings.get(self.conn, "reset_requested")
        if not token or token == settings.get(self.conn, "reset_done"):
            return
        forget = settings.get_bool(self.conn, "reset_forget_networks")
        log.warning("reset requested (token %s, forget networks and plans: %s)", token, forget)
        with write_tx(self.conn):
            maintenance.reset_usage(self.conn, forget_networks_and_plans=forget)
            settings.set_value_in_tx(self.conn, "reset_done", token)
        self.sampler.reset_caches()

    def write_heartbeat(self, state: str) -> None:
        with write_tx(self.conn):
            repo.write_heartbeat(
                self.conn,
                pid=self.sampler.pid,
                state=state,
                version=self.sampler.version,
                last_error=_summary(self.errors),
                now_utc=timeutil.now_utc_s(),
            )

    def iteration(self) -> bool:
        """One pass. Returns False when a stop was requested."""
        # Errors from steps after the previous heartbeat are reported with this one.
        carried, self.errors = self.errors, []
        if settings.get(self.conn, "stop_requested"):
            log.info("stop requested via settings")
            return False
        self.step("timezone", self.refresh_tz)
        self.step("reset", self.handle_reset)
        paused = self.read_paused()
        now = timeutil.now_utc_s()
        self.sampler.last_error = _summary(carried + self.errors)
        before = len(self.errors)
        self.step("sample", lambda: self.sampler.tick(now, paused=paused))
        if len(self.errors) > before:
            # The tick's transaction (and its heartbeat) rolled back: still show that the
            # collector is alive, with the error, so the dashboard can surface it.
            self.step("heartbeat", lambda: self.write_heartbeat("paused" if paused else "running"))
        self.step("rollup", lambda: rollup_step(self.conn))
        self.step("alerts", lambda: alerts_step(self.conn, self.platform, now, self.tz))
        mono = self.monotonic()
        if self.last_retention is None or mono - self.last_retention >= RETENTION_EVERY_S:
            self.last_retention = mono
            self.step("retention", lambda: retention_step(self.conn, now))
        return True


def _summary(errors: list[str]) -> str | None:
    return "; ".join(errors)[:1000] or None


def _clear_stale_stop(conn: sqlite3.Connection) -> None:
    """A stop token left behind (no collector was running to clear it) must not stop us."""
    try:
        if settings.get(conn, "stop_requested"):
            log.info("clearing stale stop request")
            settings.set_value(conn, "stop_requested", "")
    except sqlite3.Error:
        log.exception("could not clear a stale stop request")


def _write_stopped(conn: sqlite3.Connection, loop: _Loop) -> None:
    sampler = loop.sampler
    with write_tx(conn):
        settings.set_value_in_tx(conn, "stop_requested", "")
        repo.write_heartbeat(
            conn,
            pid=sampler.pid,
            state="stopped",
            version=sampler.version,
            last_error=_summary(loop.errors),
            now_utc=timeutil.now_utc_s(),
        )


def run(
    conn: sqlite3.Connection,
    platform: Platform,
    *,
    once: bool = False,
    lock: InstanceLock | None = None,
    sampler: Sampler | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run until stopped (or one iteration with ``once``). Returns the process exit code.

    ``lock`` (the single-instance lock taken by ``__main__``) is released on exit.
    """
    stop = platform.open_stop_signal()
    loop: _Loop | None = None
    try:
        _clear_stale_stop(conn)
        loop = _Loop(conn, platform, sampler, monotonic)
        log.info("collector started (pid %s)", loop.sampler.pid)
        while True:
            started = monotonic()
            try:
                keep_going = loop.iteration()
            except Exception:
                # Reading control settings failed (e.g. the database is locked); retry.
                log.exception("collector iteration failed")
                keep_going = True
            if not keep_going or once:
                break
            try:
                interval = settings.sample_interval_s(conn)
            except Exception:
                interval = int(settings.DEFAULTS["sample_interval_s"])
            remaining = max(0.0, interval - (monotonic() - started))
            if stop.wait(remaining):
                log.info("stop signalled")
                break
        return 0
    finally:
        if loop is not None:
            try:
                _write_stopped(conn, loop)
            except Exception:
                log.exception("could not write the stopped heartbeat")
        stop.close()
        if lock is not None:
            lock.release()
        log.info("collector stopped")


# --- crash handlers ------------------------------------------------------------------


def install_crash_handlers() -> None:
    """Log uncaught exceptions (main and worker threads) and dump hard crashes to a file."""
    global _fault_file

    def _excepthook(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        log.critical(
            "uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),  # type: ignore[arg-type]
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    try:
        from bytelog.paths import logs_dir

        if _fault_file is None:
            _fault_file = open(logs_dir() / "collector-faults.log", "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(_fault_file)
    except Exception:
        log.exception("could not enable faulthandler")
