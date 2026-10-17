from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bytelog.storage.db import open_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated, file-backed (WAL) database in a temp dir."""
    conn = open_db(tmp_path / "bytelog.db")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real %LOCALAPPDATA%\\ByteLog from tests."""
    home = tmp_path / "home"
    monkeypatch.setenv("BYTELOG_HOME", str(home))
    return home


@pytest.fixture
def lagos() -> ZoneInfo:
    return ZoneInfo("Africa/Lagos")
