"""Design tokens and the application stylesheet.

Light is the design in the brief, exactly. Dark is derived from it. Charts and custom
painted widgets read colours from :func:`tokens`, so a theme switch repaints them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    page: str
    sidebar: str
    sidebar_active: str
    sidebar_text: str
    sidebar_muted: str
    card: str
    border: str
    text: str
    muted: str
    download: str
    upload: str
    plan: str
    track: str  # empty part of progress bars
    ok: str
    warn: str
    danger: str


LIGHT = Tokens(
    page="#F2F4F6",
    sidebar="#14202B",
    sidebar_active="#24364A",
    sidebar_text="#E6EBF0",
    sidebar_muted="#8C9AA8",
    card="#FFFFFF",
    border="#DDE3E9",
    text="#14202B",
    muted="#55616C",
    download="#1F5FD1",
    upload="#F2A65A",
    plan="#C26A12",
    track="#EAEEF2",
    ok="#2E9E5B",
    warn="#D99A1E",
    danger="#D14343",
)

DARK = Tokens(
    page="#0F1820",
    sidebar="#0B141B",
    sidebar_active="#24364A",
    sidebar_text="#E6EBF0",
    sidebar_muted="#8C9AA8",
    card="#18242F",
    border="#2A3846",
    text="#E6EBF0",
    muted="#9AA6B2",
    download="#4C86F0",
    upload="#F5B577",
    plan="#E08A36",
    track="#24323F",
    ok="#3DBE72",
    warn="#E8AE3C",
    danger="#E86161",
)

SANS = "IBM Plex Sans"
MONO = "IBM Plex Mono"

_current: Tokens = LIGHT


def tokens() -> Tokens:
    return _current


def set_theme(name: str) -> Tokens:
    global _current
    _current = DARK if name == "dark" else LIGHT
    return _current


def stylesheet(t: Tokens | None = None) -> str:
    """Application-wide QSS. Widgets opt in via objectName / dynamic properties."""
    t = t or _current
    # Light: dark pill (the design). Dark: an inverted light pill, so it stays visible.
    seg_bg, seg_fg = (t.sidebar, "#FFFFFF") if t == LIGHT else (t.text, t.page)
    return f"""
    QWidget {{ font-family: "{SANS}", "Segoe UI"; font-size: 13px; color: {t.text}; }}
    QMainWindow, #Page, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {t.page}; }}
    QToolTip {{ background: {t.sidebar}; color: #FFFFFF; border: none; padding: 6px 8px;
               border-radius: 6px; }}

    #Sidebar {{ background: {t.sidebar}; }}
    #Sidebar QLabel {{ color: {t.sidebar_text}; }}
    #Sidebar QLabel[muted="true"] {{ color: {t.sidebar_muted}; font-size: 12px; }}
    #AppName {{ font-size: 16px; font-weight: 600; color: #FFFFFF; }}
    QPushButton#NavItem {{ text-align: left; padding: 9px 14px; border: none; border-radius: 8px;
        color: {t.sidebar_muted}; background: transparent; font-size: 13px; }}
    QPushButton#NavItem:hover {{ color: #FFFFFF; background: rgba(255,255,255,0.05); }}
    QPushButton#NavItem:checked {{ color: #FFFFFF; background: {t.sidebar_active}; }}
    #StatusPanel {{ background: rgba(255,255,255,0.04); border-radius: 10px; }}

    #Card {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 12px; }}
    #Card QLabel {{ background: transparent; border: none; }}
    QLabel#PageTitle {{ font-size: 22px; font-weight: 600; }}
    QLabel#CardTitle {{ font-size: 13px; font-weight: 600; }}
    QLabel[muted="true"] {{ color: {t.muted}; }}
    QLabel#BigNumber {{ font-family: "{MONO}"; font-size: 26px; font-weight: 500; }}
    QLabel#BigUnit {{ font-family: "{MONO}"; font-size: 14px; color: {t.muted}; }}
    QLabel[mono="true"] {{ font-family: "{MONO}"; }}

    QPushButton {{ padding: 7px 14px; border-radius: 8px; border: 1px solid {t.border};
        background: {t.card}; color: {t.text}; }}
    QPushButton:hover {{ border-color: {t.muted}; }}
    QPushButton:disabled {{ color: {t.muted}; }}
    QPushButton[primary="true"] {{ background: {t.download}; border-color: {t.download};
        color: #FFFFFF; }}
    QPushButton[danger="true"] {{ background: {t.danger}; border-color: {t.danger};
        color: #FFFFFF; }}

    #Segmented {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 9px; }}
    #Segmented QPushButton {{ border: none; border-radius: 7px; padding: 6px 14px;
        background: transparent; color: {t.muted}; }}
    #Segmented QPushButton:checked {{ background: {seg_bg}; color: {seg_fg}; }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{ padding: 6px 8px;
        border: 1px solid {t.border}; border-radius: 8px; background: {t.card}; color: {t.text}; }}
    QTableView, QTableWidget {{ background: {t.card}; border: 1px solid {t.border};
        border-radius: 8px; gridline-color: {t.border}; }}
    QHeaderView::section {{ background: {t.page}; color: {t.muted}; border: none;
        padding: 6px; font-weight: 600; }}
    QCheckBox {{ spacing: 8px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
    QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 3px;
        min-height: 32px; }}
    QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 3px;
        min-width: 32px; }}
    QScrollBar::handle:hover {{ background: {t.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
    """
