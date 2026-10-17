"""Select the platform implementation for the running OS."""

from __future__ import annotations

import sys
from functools import cache

from bytelog.platform.base import Platform


@cache
def get_platform() -> Platform:
    if sys.platform == "win32":
        from bytelog.platform.windows import WindowsPlatform

        return WindowsPlatform()
    raise NotImplementedError(f"ByteLog does not support {sys.platform} yet")
