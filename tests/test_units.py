from __future__ import annotations

import pytest

from bytelog.units import GB, MB, format_bytes, split_value_unit


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (0, "0 MB"),
        (1, "<0.1 MB"),
        (int(0.25 * MB), "0.2 MB"),
        (int(9.94 * MB), "9.9 MB"),
        (int(9.96 * MB), "10 MB"),  # not "10.0 MB"
        (int(10.4 * MB), "10 MB"),
        (int(1023.4 * MB), "1023 MB"),
        (int(1023.6 * MB), "1.0 GB"),  # not "1024 MB"
        (GB - 1, "1.0 GB"),
        (GB, "1.0 GB"),
        (int(23.46 * GB), "23.5 GB"),
    ],
)
def test_format_bytes_boundaries(n: int, text: str) -> None:
    assert format_bytes(n) == text


def test_split_value_unit() -> None:
    assert split_value_unit(GB - 1) == ("1.0", "GB")
    assert split_value_unit(5 * MB) == ("5.0", "MB")
