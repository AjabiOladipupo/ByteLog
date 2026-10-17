"""Register the bundled IBM Plex fonts with Qt.

Missing font files are not an error: the QSS lists Segoe UI after IBM Plex Sans, and
:func:`mono_family` falls back to Consolas, so the dashboard still renders cleanly.
"""

from __future__ import annotations

import logging

from PySide6.QtGui import QFont, QFontDatabase

from bytelog.paths import resource_path
from bytelog.ui import theme

log = logging.getLogger(__name__)

FONT_FILES = (
    "IBMPlexSans-Regular.ttf",
    "IBMPlexSans-Medium.ttf",
    "IBMPlexSans-SemiBold.ttf",
    "IBMPlexMono-Regular.ttf",
    "IBMPlexMono-Medium.ttf",
)

FALLBACK_SANS = "Segoe UI"
FALLBACK_MONO = "Consolas"

_families: set[str] = set()


def load_fonts() -> tuple[str, str]:
    """Load bundled fonts once; returns the (sans, mono) family names actually usable.

    Call after the QApplication exists.
    """
    folder = resource_path("fonts")
    for name in FONT_FILES:
        path = folder / name
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            log.warning("could not load font %s", path)
            continue
        _families.update(QFontDatabase.applicationFontFamilies(font_id))
    if theme.SANS not in _families:
        log.info("IBM Plex Sans not bundled; falling back to %s", FALLBACK_SANS)
    return sans_family(), mono_family()


def _available(family: str) -> bool:
    return family in _families or family in QFontDatabase.families()


def sans_family() -> str:
    return theme.SANS if _available(theme.SANS) else FALLBACK_SANS


def mono_family() -> str:
    return theme.MONO if _available(theme.MONO) else FALLBACK_MONO


def mono_font(point_size: float = 9.0, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(mono_family())
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    return font


def sans_font(point_size: float = 9.0, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(sans_family())
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    return font
