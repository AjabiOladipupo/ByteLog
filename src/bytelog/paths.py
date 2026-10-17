"""Filesystem locations. Every path in the app is resolved through this module.

The data directory defaults to ``%LOCALAPPDATA%\\ByteLog``. Set the ``BYTELOG_HOME``
environment variable (or pass ``--data-dir`` to either entry point) to use another
directory, e.g. for tests, demo data and development.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from bytelog import APP_NAME

ENV_HOME = "BYTELOG_HOME"


def data_dir() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "bytelog"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "bytelog.db"


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> Path:
    """Path to a bundled read-only resource (fonts, icons) in dev and under PyInstaller."""
    if is_frozen():
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "bytelog"
    else:
        root = Path(__file__).resolve().parent
    return root.joinpath("assets", *parts)


def set_data_dir_override(path: str | os.PathLike[str] | None) -> None:
    """Apply a ``--data-dir`` CLI value; also inherited by child processes."""
    if path:
        os.environ[ENV_HOME] = str(Path(path).resolve())
