"""Byte formatting shared by the UI, toasts, CSV/PDF reports and tests.

ByteLog uses binary units everywhere: 1 KB = 1024 B, 1 MB = 1024² B, 1 GB = 1024³ B.
Values under 1 GB are shown in MB, larger values in GB to one decimal place.
"""

from __future__ import annotations

KB = 1024
MB = 1024**2
GB = 1024**3


def format_bytes(n: int | float) -> str:
    # Unit and precision follow the rounded figure, so 1023.6 MB reads "1.0 GB" (not
    # "1024 MB") and 9.97 MB reads "10 MB" (not "10.0 MB").
    n = max(0, int(n))
    if n == 0:
        return "0 MB"
    mb = n / MB
    if n >= GB or int(f"{mb:.0f}") >= 1024:
        return f"{n / GB:.1f} GB"
    if mb < 0.1:
        return "<0.1 MB"
    one_decimal = f"{mb:.1f}"
    if float(one_decimal) < 10:
        return f"{one_decimal} MB"
    return f"{mb:.0f} MB"


def split_value_unit(n: int | float) -> tuple[str, str]:
    """``format_bytes`` split into number and unit, for big-number cards."""
    value, _, unit = format_bytes(n).rpartition(" ")
    return value, unit


def gb_to_bytes(gb: float) -> int:
    return int(round(gb * GB))


def bytes_to_gb(n: int) -> float:
    return n / GB


def percent(part: int, whole: int) -> float:
    return 0.0 if whole <= 0 else part * 100.0 / whole
