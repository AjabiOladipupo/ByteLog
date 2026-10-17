from __future__ import annotations

from bytelog.collector.delta import (
    FALLBACK_SPEED_BPS,
    FLAG_CAPPED,
    FLAG_CLOCK,
    FLAG_GAP,
    FLAG_RESET,
    REASON_CAPPED,
    REASON_FIRST_SIGHT,
    REASON_REBOOT,
    REASON_RESET,
    Baseline,
    cap_bytes,
    compute_delta,
    gap_threshold_s,
)
from bytelog.platform.base import Counters

T0 = 1_800_000_000
BOOT = 1_799_990_000
MBIT = 1_000_000


def base(rx: int = 1_000, tx: int = 500, ts: int = T0, boot: int = BOOT) -> Baseline:
    return Baseline(rx=rx, tx=tx, ts_utc=ts, boot_time_utc=boot)


def test_first_sight_is_baseline_only() -> None:
    d = compute_delta(None, Counters(10**9, 10**8), T0, BOOT, 10, None)
    assert (d.rx, d.tx, d.span_s, d.flags) == (0, 0, 0, 0)
    assert d.rebaseline_reason == REASON_FIRST_SIGHT


def test_normal_delta() -> None:
    d = compute_delta(base(), Counters(5_000, 900), T0 + 10, BOOT, 10, 100 * MBIT, 10.0)
    assert (d.rx, d.tx, d.span_s, d.flags) == (4_000, 400, 10, 0)
    assert d.rebaseline_reason is None
    assert d.total == 4_400


def test_fake_counter_sequence_sums_to_traffic() -> None:
    readings = [(1_000, 100), (2_000, 150), (2_000, 150), (9_000, 400), (9_500, 1_000)]
    prev = None
    total_rx = total_tx = 0
    for i, (rx, tx) in enumerate(readings):
        now = T0 + 10 * i
        d = compute_delta(prev, Counters(rx, tx), now, BOOT, 10, None, 10.0 if prev else None)
        total_rx += d.rx
        total_tx += d.tx
        prev = Baseline(rx, tx, now, BOOT)
    assert (total_rx, total_tx) == (8_500, 900)


def test_counter_reset_counts_new_raw_value() -> None:
    d = compute_delta(base(rx=50_000, tx=9_000), Counters(1_200, 300), T0 + 10, BOOT, 10, None, 10)
    assert (d.rx, d.tx) == (1_200, 300)
    assert d.flags & FLAG_RESET
    assert d.rebaseline_reason == REASON_RESET


def test_reset_in_one_direction_resets_both() -> None:
    d = compute_delta(base(rx=50_000, tx=100), Counters(60_000, 50), T0 + 10, BOOT, 10, None, 10)
    assert (d.rx, d.tx) == (60_000, 50)
    assert d.flags & FLAG_RESET


def test_reset_above_link_speed_cap_is_dropped() -> None:
    # 10 Mbit/s for 10 s * 1.25 ~= 15.6 MB; a 1 GB "since reset" value is implausible.
    d = compute_delta(
        base(rx=5 * 10**9, tx=10**9), Counters(10**9, 10), T0 + 10, BOOT, 10, 10 * MBIT, 10
    )
    assert (d.rx, d.tx) == (0, 0)
    assert d.flags & FLAG_CAPPED
    assert d.flags & FLAG_RESET
    assert d.rebaseline_reason == REASON_CAPPED


def test_cap_falls_back_to_10_gbit() -> None:
    assert cap_bytes(10, None) == cap_bytes(10, FALLBACK_SPEED_BPS) == int(10 * 1.25e9 * 1.25)
    assert cap_bytes(10, 0) == cap_bytes(10, None)


def test_ordinary_delta_not_capped_by_momentary_wifi_link_speed() -> None:
    # Link reports 144 Mbit/s now but ran faster during the interval: still counted.
    d = compute_delta(base(rx=0, tx=0), Counters(400 * 10**6, 0), T0 + 10, BOOT, 10, 144 * MBIT, 10)
    assert d.rx == 400 * 10**6
    assert not d.flags & FLAG_CAPPED


def test_ordinary_delta_above_10_gbit_is_capped() -> None:
    d = compute_delta(base(rx=0, tx=0), Counters(10**12, 0), T0 + 10, BOOT, 10, None, 10)
    assert (d.rx, d.tx) == (0, 0)
    assert d.flags & FLAG_CAPPED


def test_sleep_gap_records_whole_delta_at_wake_time() -> None:
    gap = 8 * 3600
    d = compute_delta(base(), Counters(80_000_000, 2_000_000), T0 + gap, BOOT, 10, 100 * MBIT)
    assert d.flags & FLAG_GAP
    assert d.span_s == gap
    assert (d.rx, d.tx) == (80_000_000 - 1_000, 2_000_000 - 500)


def test_gap_threshold() -> None:
    assert gap_threshold_s(10) == 60
    assert gap_threshold_s(30) == 90
    just_under = compute_delta(base(), Counters(2_000, 600), T0 + 60, BOOT, 10, None, 60)
    just_over = compute_delta(base(), Counters(2_000, 600), T0 + 61, BOOT, 10, None, 61)
    assert not just_under.flags & FLAG_GAP
    assert just_over.flags & FLAG_GAP


def test_clock_moved_back_never_negative_span() -> None:
    d = compute_delta(base(), Counters(3_000, 700), T0 - 3600, BOOT, 10, None, 10.0)
    assert d.span_s == 0
    assert d.flags & FLAG_CLOCK
    assert (d.rx, d.tx) == (2_000, 200)  # bytes still counted, not clamped away


def test_clock_back_reset_uses_monotonic_span_for_cap() -> None:
    # Wall span is 0, but 10 s really passed: a small reset value is plausible.
    d = compute_delta(base(rx=10**9), Counters(5_000_000, 0), T0 - 100, BOOT, 10, 100 * MBIT, 10.0)
    assert d.flags & FLAG_CLOCK and d.flags & FLAG_RESET
    assert not d.flags & FLAG_CAPPED
    assert d.rx == 5_000_000


def test_clock_jump_forward_detected_with_monotonic() -> None:
    d = compute_delta(base(), Counters(2_000, 600), T0 + 40, BOOT, 10, None, 10.0)
    assert d.flags & FLAG_CLOCK
    assert not d.flags & FLAG_GAP


def test_small_drift_is_not_a_clock_change() -> None:
    d = compute_delta(base(), Counters(2_000, 600), T0 + 12, BOOT, 10, None, 10.0)
    assert d.flags == 0


def test_reboot_detected_by_boot_time_across_restart() -> None:
    # Collector was off; PC rebooted; counters already climbed past the old baseline.
    d = compute_delta(
        base(rx=1_000, tx=500), Counters(70_000, 9_000), T0 + 7200, T0 + 3600, 10, None
    )
    assert (d.rx, d.tx) == (70_000, 9_000)
    assert d.flags & FLAG_RESET
    assert d.rebaseline_reason == REASON_REBOOT


def test_boot_time_jitter_is_not_a_reboot() -> None:
    for drift in (-2, 2, 119):
        d = compute_delta(base(), Counters(2_000, 600), T0 + 10, BOOT + drift, 10, None)
        assert not d.flags & FLAG_RESET
        assert (d.rx, d.tx) == (1_000, 100)


def test_boot_time_change_within_a_run_is_ignored() -> None:
    # Within one run only a clock adjustment can move psutil's boot time.
    d = compute_delta(base(), Counters(2_000, 600), T0 + 10, BOOT + 3600, 10, None, 10.0)
    assert not d.flags & FLAG_RESET
    assert (d.rx, d.tx) == (1_000, 100)


def test_never_negative() -> None:
    for rx, tx in [(0, 0), (999, 499), (1_000, 0), (0, 500)]:
        d = compute_delta(base(), Counters(rx, tx), T0 + 10, BOOT, 10, None, 10)
        assert d.rx >= 0 and d.tx >= 0


def test_reboot_after_long_downtime_with_higher_uptime() -> None:
    # Old run lasted 10 min; the PC was off overnight and has now been up 2 hours.
    prev = base(rx=1_000, tx=500, boot=T0 - 600)
    d = compute_delta(prev, Counters(70_000, 9_000), T0 + 40_000, T0 + 32_800, 10, None)
    assert d.rebaseline_reason == REASON_REBOOT
    assert (d.rx, d.tx) == (70_000, 9_000)


def test_clock_step_while_stopped_is_not_a_reboot() -> None:
    # Same boot; the wall clock was stepped forward 5 min while the collector was down,
    # which moves psutil's boot time too. Only the 10 MB / 1 MB since then is new.
    prev = Baseline(rx=1_000_000_000, tx=50_000_000, ts_utc=T0, boot_time_utc=BOOT)
    d = compute_delta(
        prev, Counters(1_010_000_000, 51_000_000), T0 + 900, BOOT + 300, 10, 866 * MBIT
    )
    assert d.rebaseline_reason is None
    assert (d.rx, d.tx) == (10_000_000, 1_000_000)
    assert not d.flags & FLAG_RESET


def test_clock_step_back_while_stopped_is_not_a_reboot() -> None:
    prev = Baseline(rx=1_000_000_000, tx=50_000_000, ts_utc=T0, boot_time_utc=BOOT)
    d = compute_delta(prev, Counters(1_010_000_000, 51_000_000), T0 + 300, BOOT - 600, 10, None)
    assert not d.flags & FLAG_RESET
    assert (d.rx, d.tx) == (10_000_000, 1_000_000)
