"""Turn two raw counter readings into a stored delta (pure; no I/O).

Rules (see docs/ARCHITECTURE.md, "Data rules"):

* First sight of an adapter: baseline only, nothing counted.
* Counter decrease: the counter restarted from 0, so the delta is the new raw value
  (bytes since the restart). Across a process restart a reboot (``rebooted_since``:
  uptime went down, or the current boot lies after the baseline) is treated the same
  way, even if the counters happen to be higher.
* Plausibility cap: a reset delta faster than ``link speed * 1.25`` over the span
  (fallback 10 Gbit/s) is impossible, so it is stored as 0 and the adapter is
  rebaselined. Ordinary deltas are checked against the 10 Gbit/s fallback only.
* Sleep gap: wall-clock elapsed > ``max(3 * interval, 60)`` s; the whole delta is
  attributed to the wake-time sample.
* Clock moved: wall elapsed < 0, or it disagrees with the monotonic clock by more than
  ``CLOCK_TOLERANCE_S`` within one run. Timestamps are never clamped.
* Never negative.
"""

from __future__ import annotations

from dataclasses import dataclass

from bytelog.platform.base import Counters

FLAG_GAP = 1
FLAG_RESET = 2
FLAG_CLOCK = 4
FLAG_SSID_UNKNOWN = 8
FLAG_CAPPED = 16

FALLBACK_SPEED_BPS = 10_000_000_000
CAP_FACTOR = 1.25
CLOCK_TOLERANCE_S = 5
BOOT_CHANGE_S = 120
MIN_GAP_S = 60

REASON_FIRST_SIGHT = "first_sight"
REASON_RESET = "counter_reset"
REASON_REBOOT = "reboot"
REASON_CAPPED = "capped"


@dataclass(frozen=True)
class Baseline:
    rx: int
    tx: int
    ts_utc: int
    boot_time_utc: int


@dataclass(frozen=True)
class DeltaResult:
    rx: int
    tx: int
    span_s: int
    flags: int
    rebaseline_reason: str | None  # why the delta is not a plain cur - prev, for logging

    @property
    def total(self) -> int:
        return self.rx + self.tx


def gap_threshold_s(interval_s: int) -> int:
    return max(3 * interval_s, MIN_GAP_S)


def cap_bytes(span_s: float, speed_bps: int | None) -> int:
    """Most bytes one direction can plausibly carry in ``span_s`` seconds."""
    speed = speed_bps if speed_bps and speed_bps > 0 else FALLBACK_SPEED_BPS
    return int(max(span_s, 1) * speed / 8 * CAP_FACTOR)


def rebooted_since(prev: Baseline, now_utc: int, boot_time_utc: int) -> bool:
    """Whether the PC rebooted after ``prev`` was taken (checked across process restarts).

    psutil derives boot time as ``time.time() - uptime``, so a wall-clock step moves it.
    Comparing boot timestamps directly would turn every clock correction made while the
    collector was down into a fake reboot that recounts all traffic since the real boot.
    Uptime itself is clock-independent: if it went down, the PC rebooted. If it went up,
    it still rebooted when the current boot lies after the baseline; a forward clock step
    can only fake that when it is larger than the old uptime.
    """
    prev_uptime = prev.ts_utc - prev.boot_time_utc
    cur_uptime = now_utc - boot_time_utc
    if cur_uptime + BOOT_CHANGE_S < prev_uptime:
        return True
    return boot_time_utc > prev.ts_utc + BOOT_CHANGE_S


def compute_delta(
    prev: Baseline | None,
    cur: Counters,
    now_utc: int,
    boot_time_utc: int,
    interval_s: int,
    speed_bps: int | None,
    mono_elapsed_s: float | None = None,
) -> DeltaResult:
    """Delta between ``prev`` and ``cur``.

    ``mono_elapsed_s`` is the monotonic time since ``prev`` was taken, or None when
    ``prev`` comes from an earlier process (restored from ``collector_state``).
    """
    if prev is None:
        return DeltaResult(0, 0, 0, 0, REASON_FIRST_SIGHT)

    wall_elapsed = now_utc - prev.ts_utc
    span_s = max(0, wall_elapsed)
    flags = 0
    if wall_elapsed < 0 or (
        mono_elapsed_s is not None and abs(wall_elapsed - mono_elapsed_s) > CLOCK_TOLERANCE_S
    ):
        flags |= FLAG_CLOCK
    if wall_elapsed > gap_threshold_s(interval_s):
        flags |= FLAG_GAP

    reason: str | None = None
    if cur.rx < prev.rx or cur.tx < prev.tx:
        rx, tx = cur.rx, cur.tx
        flags |= FLAG_RESET
        reason = REASON_RESET
    elif mono_elapsed_s is None and rebooted_since(prev, now_utc, boot_time_utc):
        # Rebooted while the collector was not running, and the counters have already
        # climbed past the old baseline. Within one run boot time cannot really change
        # (only clock adjustments move psutil's value), so this is only checked here.
        rx, tx = cur.rx, cur.tx
        flags |= FLAG_RESET
        reason = REASON_REBOOT
    else:
        rx, tx = cur.rx - prev.rx, cur.tx - prev.tx

    # The cap uses the best estimate of real elapsed time: with a backwards clock the wall
    # span is 0, but the monotonic clock still knows how long it has been. Wi-Fi link
    # speed changes from moment to moment, so ordinary deltas are only checked against
    # the 10 Gbit/s ceiling; the link speed is used for resets, where raw values matter.
    cap_span = max(span_s, mono_elapsed_s or 0, interval_s)
    limit_speed = speed_bps if reason else max(speed_bps or 0, FALLBACK_SPEED_BPS)
    limit = cap_bytes(cap_span, limit_speed)
    if rx > limit or tx > limit:
        return DeltaResult(0, 0, span_s, flags | FLAG_CAPPED, REASON_CAPPED)

    return DeltaResult(max(0, rx), max(0, tx), span_s, flags, reason)
