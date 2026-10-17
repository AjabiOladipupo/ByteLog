"""Generate ByteLog's app icon: ``src/bytelog/assets/icon.png`` and ``icon.ico``.

A rounded square in the sidebar navy with three ascending bars (download blue, download
blue, upload orange). Each ICO size is painted natively (not downscaled) so 16 px stays
crisp. The ICO container is written by hand with PNG-compressed entries, which Windows
Vista+ supports, so the result does not depend on Qt's optional ico plugin.

Run: ``uv run python scripts/make_icon.py``
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402

BACKGROUND = "#14202B"
BLUE = "#1F5FD1"
ORANGE = "#F2A65A"
ICO_SIZES = (16, 32, 48, 256)
ASSETS = Path(__file__).resolve().parent.parent / "src" / "bytelog" / "assets"

# Bars as fractions of the icon size: (height, colour), left to right, centred as a group.
BARS = ((0.30, BLUE), (0.46, BLUE), (0.62, ORANGE))
BAR_WIDTH = 0.17
BAR_GAP = 0.06
BASELINE = 0.80


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    # Small sizes use the full canvas so the shape does not look shrunken in the taskbar.
    margin = 0.0 if size <= 32 else size * 0.03
    radius = size * 0.22
    painter.setBrush(QColor(BACKGROUND))
    painter.drawRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin), radius, radius
    )

    # Small sizes snap to whole pixels with at least a 1 px gap so the bars stay distinct.
    snap = size <= 48
    width = round(BAR_WIDTH * size) if snap else BAR_WIDTH * size
    gap = max(1, round(BAR_GAP * size)) if snap else BAR_GAP * size
    left = (size - (3 * width + 2 * gap)) / 2
    baseline = BASELINE * size
    if snap:
        left, baseline = round(left), round(baseline)
    bar_radius = 0.0 if size <= 16 else size * 0.025
    for i, (height, colour) in enumerate(BARS):
        top = baseline - (round(height * size) if snap else height * size)
        rect = QRectF(left + i * (width + gap), top, width, baseline - top)
        painter.setBrush(QColor(colour))
        painter.drawRoundedRect(rect, bar_radius, bar_radius)
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("PNG encoding failed")
    buffer.close()
    return bytes(data.data())


def ico_bytes(images: list[tuple[int, bytes]]) -> bytes:
    """ICONDIR + ICONDIRENTRY[] + PNG payloads."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    payload = b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        payload += png
        offset += len(png)
    return header + entries + payload


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    ASSETS.mkdir(parents=True, exist_ok=True)
    pngs = [(size, png_bytes(render(size))) for size in ICO_SIZES]
    (ASSETS / "icon.png").write_bytes(dict(pngs)[256])
    (ASSETS / "icon.ico").write_bytes(ico_bytes(pngs))
    print(f"wrote {ASSETS / 'icon.png'} and {ASSETS / 'icon.ico'}")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
