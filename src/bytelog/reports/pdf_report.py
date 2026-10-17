"""A4 PDF usage report: summary, stacked bar chart, daily table, per-network breakdown.

Pure reportlab (platypus + reportlab.graphics), so it runs headless in a worker thread
or in tests. Colours mirror the dashboard's light theme; they are duplicated here rather
than imported from ``bytelog.ui`` to keep this package free of Qt.
"""

from __future__ import annotations

import io
import math
import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from bytelog import __version__
from bytelog.reports.fonts import ReportFonts, report_fonts
from bytelog.storage.models import Bucket, NetworkUsage
from bytelog.storage.periods import (
    daily_totals,
    date_range_label,
    day_tooltip,
    month_tooltip,
    network_usage,
)
from bytelog.storage.repo import list_networks
from bytelog.timeutil import local_zone
from bytelog.units import GB, MB, format_bytes, percent

TITLE = "ByteLog usage report"
MAX_DAILY_BARS = 62  # longer ranges are charted per month
MAX_AXIS_LABELS = 16

TEXT = colors.HexColor("#14202B")
MUTED = colors.HexColor("#55616C")
BORDER = colors.HexColor("#DDE3E9")
ZEBRA = colors.HexColor("#F5F7F9")
HEADER_BG = colors.HexColor("#F2F4F6")
DOWNLOAD = colors.HexColor("#1F5FD1")
UPLOAD = colors.HexColor("#F2A65A")

PAGE_W, PAGE_H = A4
MARGIN_X = 18 * mm
MARGIN_TOP = 18 * mm
MARGIN_BOTTOM = 20 * mm
FRAME_W = PAGE_W - 2 * MARGIN_X


@dataclass(frozen=True)
class _ReportData:
    start: date
    end: date
    days: list[Bucket]  # one per local day, zero-filled
    networks: list[NetworkUsage]  # sorted by total desc
    network_filter: str
    range_label: str

    @property
    def rx(self) -> int:
        return sum(b.rx for b in self.days)

    @property
    def tx(self) -> int:
        return sum(b.tx for b in self.days)

    @property
    def total(self) -> int:
        return self.rx + self.tx

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days + 1


def export_pdf(
    conn: sqlite3.Connection,
    path: Path,
    start: date,
    end: date,
    network_ids: list[int] | None = None,
) -> None:
    """Write the report for local days ``start``..``end`` (inclusive) to ``path``."""
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    data = _collect(conn, start, end, network_ids or None)
    fonts = report_fonts()
    pdf = _render(data, fonts)
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_bytes(pdf)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --- data -------------------------------------------------------------------------


def _collect(
    conn: sqlite3.Connection, start: date, end: date, network_ids: list[int] | None
) -> _ReportData:
    days = daily_totals(conn, start, end, network_ids)
    networks = network_usage(conn, start, end)
    if network_ids:
        wanted = set(network_ids)
        networks = [n for n in networks if n.network_id in wanted]
    return _ReportData(
        start=start,
        end=end,
        days=days,
        networks=[n for n in networks if n.total > 0],
        network_filter=_network_filter_label(conn, network_ids),
        range_label=date_range_label(start, end),
    )


def _network_filter_label(conn: sqlite3.Connection, network_ids: list[int] | None) -> str:
    if not network_ids:
        return "All networks"
    wanted = set(network_ids)
    labels = [n.label for n in list_networks(conn) if n.id in wanted]
    return ", ".join(labels) if labels else "Selected networks (no longer present)"


def _day(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _days_text(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _chart_buckets(days: Sequence[Bucket]) -> tuple[list[Bucket], bool]:
    """Daily buckets, or per-month buckets when the range is too long to read per day."""
    if len(days) <= MAX_DAILY_BARS:
        return list(days), False
    months: dict[date, list[int]] = {}
    for b in days:
        acc = months.setdefault(_day(b.start).replace(day=1), [0, 0])
        acc[0] += b.rx
        acc[1] += b.tx
    return [
        Bucket(f"{month_tooltip(first)[:3]} {first.year}", month_tooltip(first), first, rx, tx)
        for first, (rx, tx) in months.items()
    ], True


# --- rendering --------------------------------------------------------------------


def _styles(fonts: ReportFonts) -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName=fonts.sans, fontSize=9, leading=12, textColor=TEXT)
    return {
        "title": ParagraphStyle(
            "title", parent=base, fontName=fonts.sans_bold, fontSize=20, leading=24
        ),
        "meta": ParagraphStyle("meta", parent=base, textColor=MUTED, leading=13),
        "h2": ParagraphStyle(
            "h2",
            parent=base,
            fontName=fonts.sans_bold,
            fontSize=12,
            leading=16,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "body": base,
        "muted": ParagraphStyle("muted", parent=base, textColor=MUTED),
        "card_label": ParagraphStyle(
            "card_label", parent=base, fontSize=8, leading=10, textColor=MUTED
        ),
        "card_value": ParagraphStyle(
            "card_value", parent=base, fontName=fonts.mono_medium, fontSize=13, leading=17
        ),
        "card_detail": ParagraphStyle(
            "card_detail", parent=base, fontSize=7.5, leading=10, textColor=MUTED
        ),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11, alignment=TA_LEFT),
    }


def _render(data: _ReportData, fonts: ReportFonts) -> bytes:
    styles = _styles(fonts)
    generated = datetime.now(local_zone())
    story: list[Flowable] = [
        Paragraph(TITLE, styles["title"]),
        Spacer(1, 4),
        Paragraph(
            f"{escape(data.range_label)} · {_days_text(data.day_count)}",
            styles["meta"],
        ),
        Paragraph(f"Networks: {escape(data.network_filter)}", styles["meta"]),
        Paragraph(
            f"Generated {day_tooltip(generated.date())} at {generated:%H:%M}", styles["meta"]
        ),
        Spacer(1, 10),
        _summary_table(data, fonts, styles),
    ]

    buckets, monthly = _chart_buckets(data.days)
    story.append(Paragraph("Usage by month" if monthly else "Usage by day", styles["h2"]))
    if data.total > 0:
        story.append(_chart(buckets, fonts))
    else:
        story.append(Paragraph("No usage was recorded in this range.", styles["muted"]))

    story.append(Paragraph("Daily usage", styles["h2"]))
    story.append(_daily_table(data, fonts))

    story.append(Paragraph("By network", styles["h2"]))
    if data.networks:
        story.append(_network_table(data, fonts, styles))
    else:
        story.append(Paragraph("No usage was recorded in this range.", styles["muted"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title=TITLE,
        author="ByteLog",
        subject=data.range_label,
        creator=f"ByteLog {__version__}",
    )
    doc.build(story, canvasmaker=_numbered_canvas(f"{TITLE} · {data.range_label}", fonts.sans))
    return buffer.getvalue()


def _summary_table(
    data: _ReportData, fonts: ReportFonts, styles: dict[str, ParagraphStyle]
) -> Table:
    busiest = max(data.days, key=lambda b: b.total, default=None)
    if busiest is None or busiest.total == 0:
        busiest_value, busiest_detail = "—", "No usage"
    else:
        busiest_value = format_bytes(busiest.total)
        busiest_detail = day_tooltip(_day(busiest.start))
    average = data.total // data.day_count
    cards = [
        ("Total used", format_bytes(data.total), "Download + upload"),
        ("Downloaded", format_bytes(data.rx), f"{percent(data.rx, data.total):.0f}% of total"),
        ("Uploaded", format_bytes(data.tx), f"{percent(data.tx, data.total):.0f}% of total"),
        ("Daily average", format_bytes(average), f"over {_days_text(data.day_count)}"),
        ("Busiest day", busiest_value, busiest_detail),
    ]
    row = [
        [
            Paragraph(escape(label), styles["card_label"]),
            Paragraph(escape(value), styles["card_value"]),
            Paragraph(escape(detail), styles["card_detail"]),
        ]
        for label, value, detail in cards
    ]
    table = Table([row], colWidths=[FRAME_W / len(cards)] * len(cards))
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.75, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.75, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def _axis_formatter(unit_name: str) -> Callable[[float], str]:
    def fmt(value: float) -> str:
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return f"{text} {unit_name}"

    return fmt


def _chart(buckets: list[Bucket], fonts: ReportFonts) -> Drawing:
    height = 62 * mm
    drawing = Drawing(FRAME_W, height)
    tallest = max(b.total for b in buckets)
    unit, unit_name = (GB, "GB") if tallest >= GB else (MB, "MB")

    chart = VerticalBarChart()
    chart.x = 40
    chart.y = 22
    chart.width = FRAME_W - chart.x - 4
    chart.height = height - chart.y - 22
    chart.data = [[b.rx / unit for b in buckets], [b.tx / unit for b in buckets]]
    chart.categoryAxis.style = "stacked"
    chart.bars[0].fillColor = DOWNLOAD
    chart.bars[1].fillColor = UPLOAD
    chart.bars.strokeColor = None
    chart.bars.strokeWidth = 0
    chart.barSpacing = 0
    chart.groupSpacing = 6 if len(buckets) <= 31 else 3 if len(buckets) <= MAX_DAILY_BARS else 8

    step = math.ceil(len(buckets) / MAX_AXIS_LABELS)
    chart.categoryAxis.categoryNames = [
        b.label if i % step == 0 else "" for i, b in enumerate(buckets)
    ]
    chart.categoryAxis.labels.fontName = fonts.sans
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.fillColor = MUTED
    chart.categoryAxis.labels.boxAnchor = "n"
    chart.categoryAxis.labels.dy = -3
    chart.categoryAxis.strokeColor = BORDER
    chart.categoryAxis.visibleTicks = False

    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontName = fonts.mono
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.labels.fillColor = MUTED
    chart.valueAxis.labelTextFormat = _axis_formatter(unit_name)
    chart.valueAxis.visibleAxis = False
    chart.valueAxis.visibleTicks = False
    chart.valueAxis.visibleGrid = True
    chart.valueAxis.gridStrokeColor = BORDER
    chart.valueAxis.gridStrokeWidth = 0.5
    chart.valueAxis.maximumTicks = 6
    drawing.add(chart)

    legend = Legend()
    legend.colorNamePairs = [(DOWNLOAD, "Download"), (UPLOAD, "Upload")]
    legend.fontName = fonts.sans
    legend.fontSize = 8
    legend.fillColor = TEXT
    legend.alignment = "right"
    legend.columnMaximum = 1
    legend.deltax = 62
    legend.dx = 7
    legend.dy = 7
    legend.strokeColor = None
    legend.swdx = 0
    legend.boxAnchor = "ne"
    legend.x = FRAME_W - 120
    legend.y = height - 2
    drawing.add(legend)

    tall = max(buckets, key=lambda b: b.total)
    drawing.add(
        String(
            0,
            height - 9,
            f"Tallest bar: {tall.tooltip_title}, {format_bytes(tall.total)}",
            fontName=fonts.sans,
            fontSize=8,
            fillColor=MUTED,
        )
    )
    return drawing


_TABLE_BASE: list[tuple[Any, ...]] = [
    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
    ("TEXTCOLOR", (0, 0), (-1, -1), TEXT),
    ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
    ("FONTSIZE", (0, 0), (-1, 0), 8),
    ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
    ("LINEBELOW", (0, 0), (-1, 0), 0.75, BORDER),
    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("TOPPADDING", (0, 0), (-1, -1), 3.5),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ("BOX", (0, 0), (-1, -1), 0.75, BORDER),
]


def _daily_table(data: _ReportData, fonts: ReportFonts) -> Table:
    rows: list[list[Any]] = [["Date", "Download", "Upload", "Total"]]
    for b in data.days:
        rows.append(
            [
                day_tooltip(_day(b.start)),
                format_bytes(b.rx),
                format_bytes(b.tx),
                format_bytes(b.total),
            ]
        )
    rows.append(["Total", format_bytes(data.rx), format_bytes(data.tx), format_bytes(data.total)])
    widths = [FRAME_W * 0.4, FRAME_W * 0.2, FRAME_W * 0.2, FRAME_W * 0.2]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                *_TABLE_BASE,
                ("FONTNAME", (0, 0), (-1, 0), fonts.sans_bold),
                ("FONTNAME", (0, 1), (0, -1), fonts.sans),
                ("FONTNAME", (1, 1), (-1, -1), fonts.mono),
                ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, ZEBRA]),
                ("LINEABOVE", (0, -1), (-1, -1), 0.75, BORDER),
                ("FONTNAME", (0, -1), (0, -1), fonts.sans_bold),
                ("FONTNAME", (1, -1), (-1, -1), fonts.mono_medium),
            ]
        )
    )
    return table


def _network_table(
    data: _ReportData, fonts: ReportFonts, styles: dict[str, ParagraphStyle]
) -> Flowable:
    total = sum(n.total for n in data.networks)
    rows: list[list[Any]] = [["Network", "Download", "Upload", "Total", "Share"]]
    for n in data.networks:
        label = escape(n.label)
        if n.is_metered:
            label += ' <font color="#55616C">· metered</font>'
        rows.append(
            [
                Paragraph(label, styles["cell"]),
                format_bytes(n.rx),
                format_bytes(n.tx),
                format_bytes(n.total),
                f"{percent(n.total, total):.1f}%",
            ]
        )
    widths = [FRAME_W * 0.36, FRAME_W * 0.16, FRAME_W * 0.16, FRAME_W * 0.16, FRAME_W * 0.16]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                *_TABLE_BASE,
                ("FONTNAME", (0, 0), (-1, 0), fonts.sans_bold),
                ("FONTNAME", (1, 1), (-1, -1), fonts.mono),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
            ]
        )
    )
    return KeepTogether([table]) if len(rows) <= 20 else table


def _numbered_canvas(footer: str, font: str) -> type[Canvas]:
    """Canvas class that defers each page so the footer can say "Page n of N"."""

    class NumberedCanvas(Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._pages: list[dict[str, Any]] = []

        def showPage(self) -> None:  # noqa: N802 - reportlab API
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            count = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._footer(count)
                super().showPage()
            super().save()

        def _footer(self, count: int) -> None:
            y = MARGIN_BOTTOM - 9 * mm
            self.saveState()
            self.setStrokeColor(BORDER)
            self.setLineWidth(0.5)
            self.line(MARGIN_X, y + 10, PAGE_W - MARGIN_X, y + 10)
            self.setFont(font, 7.5)
            self.setFillColor(MUTED)
            self.drawString(MARGIN_X, y, footer)
            self.drawRightString(PAGE_W - MARGIN_X, y, f"Page {self._pageNumber} of {count}")
            self.restoreState()

    return NumberedCanvas
