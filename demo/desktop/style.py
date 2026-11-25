"""How the app looks: colours, fonts, icons, the stylesheet, and a few small widgets.

The colours are the web page's (``demo/static/style.css``), so the app and the page look
alike. Everything here is drawn by the app itself (icons as SVG, the switch, the arrows
of the legend), so it looks the same whatever theme the desktop has.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QApplication, QSizePolicy, QWidget

# -- colours --------------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#f6f7f9",
        "panel": "#ffffff",
        "panel-2": "#eef0f4",
        "ink": "#12161c",
        "ink-2": "#4a5361",
        "ink-3": "#7b8492",
        "line": "#d3d8e0",
        "line-2": "#e4e8ee",
        "accent": "#1f6feb",
        "focus": "#ff9f1c",
        "failed": "#d0312d",
        "done": "#1a8f4c",
        "waiting": "#c98a00",
        "neutral": "#6b7a90",
        "shadow": "#1a2230",
    },
    "dark": {
        "bg": "#0d1117",
        "panel": "#161b22",
        "panel-2": "#1f2630",
        "ink": "#eef2f6",
        "ink-2": "#a5b0be",
        "ink-3": "#6e7a8a",
        "line": "#303844",
        "line-2": "#242b35",
        "accent": "#58a6ff",
        "focus": "#ff9f1c",
        "failed": "#f0524d",
        "done": "#2ea860",
        "waiting": "#e3a700",
        "neutral": "#8b98ab",
        "shadow": "#000000",
    },
}

# The colour of each kind of class, as the web page tints its boxes.
KIND_COLOUR = {
    "protocol": "accent",
    "exception": "failed",
    "model": "done",
    "dataclass": "done",
    "enumeration": "waiting",
}


def mix(colour: str, share: float, into: str) -> QColor:
    """``color-mix(in srgb, colour share, into)``."""
    a, b = QColor(colour), QColor(into)
    return QColor.fromRgbF(
        a.redF() * share + b.redF() * (1 - share),
        a.greenF() * share + b.greenF() * (1 - share),
        a.blueF() * share + b.blueF() * (1 - share),
    )


def is_dark(colours: dict[str, str]) -> bool:
    return QColor(colours["bg"]).lightness() < 128


def kind_colour(colours: dict[str, str], kind: str) -> str:
    return colours[KIND_COLOUR.get(kind, "neutral")]


# -- fonts ----------------------------------------------------------------------------

_SANS = ("Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans")
_MONO = ("Source Code Pro", "Ubuntu Mono", "DejaVu Sans Mono")


def _family(choices: tuple[str, ...]) -> str:
    have = set(QFontDatabase.families())
    return next((f for f in choices if f in have), choices[-1])


def sans(px: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The interface's font, at ``px`` pixels."""
    font = QFont(_family(_SANS))
    font.setPixelSize(round(px))
    font.setWeight(weight)
    return font


def caps(px: float) -> QFont:
    """Small capitals for headings: bold, spaced out."""
    font = sans(px, QFont.Weight.Bold)
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
    return font


def mono(px: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    """The font of code, at ``px`` pixels."""
    font = QFont(_family(_MONO))
    font.setPixelSize(round(px))
    font.setWeight(weight)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


# -- icons: simple line drawings, 24 x 24, in the colour asked for ------------------------

_ICONS = {
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    "close": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "fit": '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.5H15"/>',
    "copy": '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/>'
    '<path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
    "save": '<path d="M12 4.5v10M7.5 10.5l4.5 4.5 4.5-4.5M5 19.5h14"/>',
    "code": '<path d="M9 7.5l-5 4.5 5 4.5M15 7.5l5 4.5-5 4.5"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/>'
    '<circle cx="12" cy="12" r="3"/>',
    "neighbours": '<circle cx="12" cy="12" r="3"/><circle cx="4.5" cy="5.5" r="2"/>'
    '<circle cx="19.5" cy="5.5" r="2"/><circle cx="12" cy="20.5" r="2"/>'
    '<path d="M9.8 10L6 7M14.2 10L18 7M12 15v3.5"/>',
    "centre": '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="1.5"/>'
    '<path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
    "open": '<path d="M14 4.5h5.5V10M19.5 4.5L11 13"/>'
    '<path d="M17.5 14v4.5A1.5 1.5 0 0 1 16 20H6a1.5 1.5 0 0 1-1.5-1.5v-10'
    'A1.5 1.5 0 0 1 6 7h4.5"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2'
    'M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    "moon": '<path d="M19.5 14.5A7.5 7.5 0 1 1 9.5 4.5a6 6 0 0 0 10 10z"/>',
    "system": '<rect x="3.5" y="4.5" width="17" height="11.5" rx="2"/><path d="M8.5 20h7M12 16v4"/>',
    "check": '<path d="M5.5 12.5l4 4 9-9.5"/>',
}


def svg(name: str, colour: str, width: float = 2.0) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{colour}" stroke-width="{width}" stroke-linecap="round" '
        f'stroke-linejoin="round">{_ICONS[name]}</svg>'
    )


def icon(name: str, colour: str, size: int = 18) -> QIcon:
    """An icon drawn sharp at the screen's pixel ratio."""
    app = QApplication.instance()
    ratio = app.devicePixelRatio() if isinstance(app, QApplication) else 1.0
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg(name, colour).encode())).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


# -- the stylesheet ----------------------------------------------------------------------


def stylesheet(c: dict[str, str], check_svg: Path) -> str:
    """The widgets' look; ``check_svg`` is the tick drawn in a checked box."""
    hover = mix(c["ink"], 0.06, c["panel"]).name()
    pressed = mix(c["ink"], 0.11, c["panel"]).name()
    selected = mix(c["accent"], 0.14, c["panel"]).name()
    check = check_svg.as_posix()
    return f"""
    QWidget {{ color: {c["ink"]}; }}
    QMainWindow, QDialog {{ background: {c["bg"]}; }}
    QToolTip {{ background: {c["panel"]}; color: {c["ink"]}; border: 1px solid {c["line"]};
                border-radius: 6px; padding: 5px 8px; }}

    QWidget#side, QScrollArea#sidescroll, QWidget#detail {{ background: {c["panel"]}; }}
    QScrollArea#sidescroll {{ border: none; border-right: 1px solid {c["line-2"]}; }}
    QWidget#detail {{ border-left: 1px solid {c["line-2"]}; }}
    QSplitter::handle {{ background: {c["line-2"]}; }}
    QSplitter::handle:horizontal {{ width: 1px; }}

    QLabel#apptitle {{ font-size: 15px; font-weight: 700; }}
    QLabel#subtitle, QLabel#muted {{ color: {c["ink-3"]}; }}
    QLabel#section {{ color: {c["ink-3"]}; padding-top: 14px; padding-bottom: 2px; }}
    QLabel#count {{ color: {c["ink-3"]}; background: {c["panel-2"]}; border-radius: 8px;
                    padding: 0px 7px; font-size: 11px; }}
    QLabel#title {{ font-size: 22px; font-weight: 700; }}
    QLabel#where {{ color: {c["ink-3"]}; }}

    QLineEdit {{ background: {c["bg"]}; border: 1px solid {c["line"]}; border-radius: 8px;
                 padding: 7px 8px; selection-background-color: {c["accent"]}; }}
    QLineEdit:focus {{ border: 1px solid {c["accent"]}; background: {c["panel"]}; }}

    QPushButton {{ background: {c["panel"]}; border: 1px solid {c["line"]}; border-radius: 8px;
                   padding: 6px 12px; }}
    QPushButton:hover {{ background: {hover}; }}
    QPushButton:pressed {{ background: {pressed}; }}
    QPushButton#primary {{ background: {c["accent"]}; border-color: {c["accent"]}; color: #ffffff;
                           font-weight: 700; }}

    QToolButton {{ background: transparent; border: none; border-radius: 7px; padding: 5px; }}
    QToolButton:hover {{ background: {hover}; }}
    QToolButton:pressed {{ background: {pressed}; }}
    QToolButton:checked {{ background: {selected}; }}
    QToolButton#link {{ color: {c["accent"]}; padding: 2px 6px; font-weight: 600; }}

    QCheckBox {{ spacing: 9px; padding: 3px 0px; }}
    QCheckBox::indicator {{ width: 15px; height: 15px; border: 1.5px solid {c["line"]};
                            border-radius: 4px; background: {c["panel"]}; }}
    QCheckBox::indicator:hover {{ border-color: {c["accent"]}; }}
    QCheckBox::indicator:checked {{ background: {c["accent"]}; border-color: {c["accent"]};
                                    image: url("{check}"); }}

    QFrame#floating {{ background: {c["panel"]}; border: 1px solid {c["line"]};
                       border-radius: 10px; }}
    QLabel#pill {{ background: {c["panel"]}; border: 1px solid {c["line"]}; border-radius: 12px;
                   padding: 4px 12px; color: {c["ink-2"]}; }}
    QLabel#zoom {{ color: {c["ink-2"]}; min-width: 44px; }}
    QFrame#rule {{ background: {c["line-2"]}; max-height: 1px; min-height: 1px; border: none; }}
    QFrame#vrule {{ background: {c["line-2"]}; max-width: 1px; min-width: 1px; border: none; }}

    QTextBrowser, QPlainTextEdit {{ background: transparent; border: none; }}
    QPlainTextEdit#code {{ background: {c["panel"]}; border: 1px solid {c["line-2"]};
                           border-radius: 10px; padding: 6px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle {{ background: {c["line"]}; border-radius: 3px; min-height: 30px;
                          min-width: 30px; }}
    QScrollBar::handle:hover {{ background: {c["ink-3"]}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QMenu {{ background: {c["panel"]}; border: 1px solid {c["line"]}; border-radius: 8px; padding: 4px; }}
    QMenu::item {{ padding: 6px 14px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {selected}; }}
    """


# -- a switch ----------------------------------------------------------------------------


class Switch(QAbstractButton):
    """An on/off switch, for the settings that change what is drawn."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.colours = THEMES["light"]
        self._knob = 0.0  # 0 off, 1 on
        self._slide = QVariantAnimation(self)
        self._slide.setDuration(140)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.valueChanged.connect(self._moved)
        self.toggled.connect(self._toggled)

    def _toggled(self, on: bool) -> None:
        self._slide.stop()
        self._slide.setStartValue(self._knob)
        self._slide.setEndValue(1.0 if on else 0.0)
        self._slide.start()

    def _moved(self, value: float) -> None:
        self._knob = float(value)
        self.update()

    def setChecked(self, on: bool) -> None:  # noqa: N802 (Qt's name)
        super().setChecked(on)
        self._slide.stop()
        self._knob = 1.0 if on else 0.0
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt's name)
        return QSize(200, 28)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt's name)
        c = self.colours
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = 34.0, 20.0
        track = QRectF(self.width() - w - 2, (self.height() - h) / 2, w, h)
        off, on = QColor(c["line"]), QColor(c["accent"])
        t = self._knob
        colour = QColor.fromRgbF(
            off.redF() + (on.redF() - off.redF()) * t,
            off.greenF() + (on.greenF() - off.greenF()) * t,
            off.blueF() + (on.blueF() - off.blueF()) * t,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawRoundedRect(track, h / 2, h / 2)
        r = h / 2 - 3
        x = track.left() + 3 + r + t * (w - 6 - 2 * r)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QPointF(x, track.center().y()), r, r)
        painter.setPen(QColor(c["ink"]))
        painter.setFont(self.font())
        text = QRectF(0, 0, self.width() - w - 12, self.height())
        painter.drawText(
            text,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self.text(),
        )


# -- arrows, as the diagram and its legend draw them ------------------------------------------

ARROW = 10.0  # length of an arrowhead


def draw_head(
    painter: QPainter,
    kind: str,
    tip: QPointF,
    back: QPointF,
    line: QColor,
    fill: QColor,
) -> None:
    """The head of an arrow at ``tip``, coming from ``back``.

    A hollow triangle for inherits and implements (UML's generalisation), an open
    head for holds and uses.
    """
    dx, dy = tip.x() - back.x(), tip.y() - back.y()
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    base = QPointF(tip.x() - ux * ARROW, tip.y() - uy * ARROW)
    side = QPointF(-uy * ARROW * 0.55, ux * ARROW * 0.55)
    pen = QPen(line, painter.pen().widthF())
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    if kind in ("inherits", "implements"):
        painter.setBrush(fill)
        painter.drawPolygon(QPolygonF([tip, base + side, base - side]))
    else:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(QPolygonF([base + side, tip, base - side]))


LINE_STYLE = {
    "inherits": Qt.PenStyle.SolidLine,
    "implements": Qt.PenStyle.DashLine,
    "holds": Qt.PenStyle.SolidLine,
    "uses": Qt.PenStyle.DotLine,
}


def legend_arrow(
    kind: str, colours: dict[str, str], width: int = 44, height: int = 14
) -> QPixmap:
    """A short arrow of ``kind``, as the diagram draws it, for the legend."""
    app = QApplication.instance()
    ratio = app.devicePixelRatio() if isinstance(app, QApplication) else 1.0
    pixmap = QPixmap(round(width * ratio), round(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    line = QColor(colours["ink-2"])
    painter.setPen(QPen(line, 1.4, LINE_STYLE[kind]))
    y = height / 2
    start, end = QPointF(2, y), QPointF(width - 3, y)
    path = QPainterPath(start)
    path.lineTo(end)
    painter.drawPath(path)
    painter.setPen(QPen(line, 1.4))
    draw_head(painter, kind, end, start, line, QColor(colours["panel"]))
    painter.end()
    return pixmap


def swatch(colours: dict[str, str], kind: str, size: int = 12) -> QPixmap:
    """A small square in a kind's colour, for the legend."""
    app = QApplication.instance()
    ratio = app.devicePixelRatio() if isinstance(app, QApplication) else 1.0
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    colour = kind_colour(colours, kind)
    painter.setPen(QPen(QColor(colour), 1.2))
    painter.setBrush(mix(colour, 0.28 if is_dark(colours) else 0.18, colours["panel"]))
    painter.drawRoundedRect(QRectF(0.6, 0.6, size - 1.2, size - 1.2), 3, 3)
    painter.end()
    return pixmap
