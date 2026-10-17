"""Rotating file logging. Each process logs to its own file, because Windows cannot
rename a log file that another process holds open."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from bytelog.paths import logs_dir


def setup_logging(process_name: str, level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        logs_dir() / f"{process_name}.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # Windowed (PyInstaller) builds have no console: sys.stderr is None there.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)
    return logging.getLogger(f"bytelog.{process_name}")
