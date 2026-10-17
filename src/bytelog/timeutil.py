"""Time helpers. All stored timestamps are integer UTC epoch seconds.

The local timezone always comes from the system (via tzlocal), never a hardcoded
zone. ``tzlocal`` caches the zone, so long-running processes call ``local_zone(refresh=True)``
about once a minute to notice a timezone change.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta, tzinfo

import tzlocal

log = logging.getLogger(__name__)

_warned_fallback = False


def local_zone(refresh: bool = False) -> tzinfo:
    """The system time zone.

    tzlocal raises when Windows' zone cannot be mapped to an IANA name (an unmapped
    zone, or automatic DST turned off in a zone that has DST). The fallback is the
    current fixed UTC offset, so local days stay right until the next refresh.
    """
    global _warned_fallback
    try:
        if refresh:
            tzlocal.reload_localzone()
        return tzlocal.get_localzone()
    except Exception as exc:
        if not _warned_fallback:
            _warned_fallback = True
            log.warning("could not resolve the local time zone (%s); using a fixed offset", exc)
        return fixed_offset_zone()


def fixed_offset_zone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or UTC


def now_utc_s() -> int:
    return int(time.time())


def utc_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def to_local(ts: int, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def utc_offset_s(ts: int, tz: tzinfo) -> int:
    offset = to_local(ts, tz).utcoffset()
    return int(offset.total_seconds()) if offset else 0


def local_date(ts: int, tz: tzinfo) -> date:
    return to_local(ts, tz).date()


def local_midnight_utc_s(day: date, tz: tzinfo) -> int:
    """UTC epoch seconds of local midnight starting ``day``."""
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())


def day_bounds_utc_s(day: date, tz: tzinfo) -> tuple[int, int]:
    """Half-open [start, end) UTC seconds of the local calendar day."""
    return local_midnight_utc_s(day, tz), local_midnight_utc_s(day + timedelta(days=1), tz)


def iso_day(day: date) -> str:
    return day.isoformat()


def parse_day(text: str) -> date:
    return date.fromisoformat(text)
