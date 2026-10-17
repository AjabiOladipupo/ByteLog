"""Register the bundled IBM Plex TTFs with reportlab (falls back to the core PDF fonts).

reportlab embeds a subset of each TTF in the PDF, which the SIL OFL allows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from bytelog.paths import resource_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportFonts:
    sans: str
    sans_medium: str
    sans_bold: str
    mono: str
    mono_medium: str

    @property
    def is_plex(self) -> bool:
        return self.sans == "IBMPlexSans"


FALLBACK = ReportFonts(
    sans="Helvetica",
    sans_medium="Helvetica",
    sans_bold="Helvetica-Bold",
    mono="Courier",
    mono_medium="Courier-Bold",
)

# reportlab font name -> bundled file
PLEX_FILES: dict[str, str] = {
    "IBMPlexSans": "IBMPlexSans-Regular.ttf",
    "IBMPlexSans-Medium": "IBMPlexSans-Medium.ttf",
    "IBMPlexSans-SemiBold": "IBMPlexSans-SemiBold.ttf",
    "IBMPlexMono": "IBMPlexMono-Regular.ttf",
    "IBMPlexMono-Medium": "IBMPlexMono-Medium.ttf",
}


@cache
def report_fonts() -> ReportFonts:
    """Register Plex once per process and return the font names to use."""
    folder = resource_path("fonts")
    try:
        for name, filename in PLEX_FILES.items():
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(folder / filename)))
        pdfmetrics.registerFontFamily(
            "IBMPlexSans",
            normal="IBMPlexSans",
            bold="IBMPlexSans-SemiBold",
            italic="IBMPlexSans",
            boldItalic="IBMPlexSans-SemiBold",
        )
    except Exception as exc:  # missing/corrupt file: the report still renders
        log.warning("IBM Plex fonts unavailable for PDF reports, using Helvetica: %s", exc)
        return FALLBACK
    return ReportFonts(
        sans="IBMPlexSans",
        sans_medium="IBMPlexSans-Medium",
        sans_bold="IBMPlexSans-SemiBold",
        mono="IBMPlexMono",
        mono_medium="IBMPlexMono-Medium",
    )


def register_fonts() -> tuple[str, str]:
    """(sans, mono) reportlab font names: IBM Plex, or Helvetica/Courier as a fallback."""
    fonts = report_fonts()
    return fonts.sans, fonts.mono
