from __future__ import annotations

import struct

import pytest
from reportlab.pdfbase.ttfonts import TTFont

from bytelog.paths import resource_path

FONT_FILES = (
    "IBMPlexSans-Regular.ttf",
    "IBMPlexSans-Medium.ttf",
    "IBMPlexSans-SemiBold.ttf",
    "IBMPlexMono-Regular.ttf",
    "IBMPlexMono-Medium.ttf",
)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("name", FONT_FILES)
def test_font_is_static_truetype_and_loads(name: str):
    path = resource_path("fonts", name)
    assert path.read_bytes()[:4] == b"\x00\x01\x00\x00"
    font = TTFont(f"test-{path.stem}", str(path))
    assert font.face.name.startswith(b"IBMPlex")


def test_font_license_is_bundled():
    text = resource_path("fonts", "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in text


def test_icon_png_is_256_square():
    data = resource_path("icon.png").read_bytes()
    assert data[:8] == PNG_MAGIC
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (256, 256)


def test_icon_ico_has_all_sizes():
    data = resource_path("icon.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = set()
    for i in range(count):
        w, _h, _c, _r, _planes, _bpp, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * i : 22 + 16 * i]
        )
        sizes.add(w or 256)
        assert data[offset : offset + 8] == PNG_MAGIC
        assert offset + length <= len(data)
    assert sizes == {16, 32, 48, 256}


def test_icons_load_in_qt(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qtgui = pytest.importorskip("PySide6.QtGui")
    qtwidgets = pytest.importorskip("PySide6.QtWidgets")
    # A widget QApplication (not a bare QGuiApplication), so widget tests later in the same
    # session can reuse it.
    app = qtgui.QGuiApplication.instance() or qtwidgets.QApplication([])
    image = qtgui.QImage(str(resource_path("icon.png")))
    assert not image.isNull() and image.width() == 256
    icon = qtgui.QIcon(str(resource_path("icon.ico")))
    assert not icon.isNull()
    assert app is not None
