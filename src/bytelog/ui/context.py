"""Shared state handed to every dashboard page."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QObject, Signal

from bytelog.platform.base import Platform


class AppContext(QObject):
    """One per dashboard process.

    Pages read through ``conn`` (short autocommit reads) and write only settings, plans,
    network labels/metered flags and adapter overrides, always via ``write_tx``.
    """

    data_changed = Signal()  # usage data changed outside the periodic refresh (e.g. a reset)
    plans_changed = Signal()  # a plan was created, edited or deleted
    networks_changed = Signal()  # a network label/metered flag or adapter override changed
    settings_changed = Signal(str)  # key of the setting that changed
    theme_changed = Signal(str)  # "light" | "dark"
    navigate = Signal(str)  # page key: overview | reports | networks | plans | settings

    def __init__(self, conn: sqlite3.Connection, platform: Platform) -> None:
        super().__init__()
        self.conn = conn
        self.platform = platform
