"""What the canvas draws: class boxes, arrows and areas, as Qt graphics items.

Each item paints itself from the layout (``diagram.Layout``); none of them knows the
window. Sizes are measured here before the layout, so Graphviz places boxes of
exactly the size they are drawn.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from .diagram import Member

# The web page's colours (demo/static/style.css), light and dark.
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#f6f7f9",
        "panel": "#ffffff",
        "panel-2": "#eef0f4",
        "ink": "#12161c",
        "ink-2": "#4a5361",
        "line": "#d3d8e0",
        "accent": "#1f6feb",
        "focus": "#ff9f1c",
        "failed": "#d0312d",
        "done": "#1a8f4c",
        "waiting": "#c98a00",
    },
    "dark": {
        "bg": "#0d1117",
        "panel": "#161b22",
        "panel-2": "#1f2630",
        "ink": "#eef2f6",
        "ink-2": "#a5b0be",
        "line": "#303844",
        "accent": "#58a6ff",
        "focus": "#ff9f1c",
        "failed": "#f0524d",
        "done": "#2ea860",
        "waiting": "#e3a700",
    },
}

# A kind's fill: a share of a colour mixed into the panel, as classes.css does.
_KIND_FILL = {
    "protocol": ("accent", 0.14),
    "exception": ("failed", 0.12),
    "model": ("done", 0.10),
    "dataclass": ("done", 0.10),
    "enumeration": ("waiting", 0.14),
}


def mix(colour: str, share: float, into: str) -> QColor:
    """``color-mix(in srgb, colour share, into)``."""
    a, b = QColor(colour), QColor(into)
    return QColor.fromRgbF(
        a.redF() * share + b.redF() * (1 - share),
        a.greenF() * share + b.greenF() * (1 - share),
        a.blueF() * share + b.blueF() * (1 - share),
    )


@dataclass(frozen=True)
class Fonts:
    """The diagram's fonts, in pixels so a box measures the same on every screen."""

    name: QFont
    kind: QFont
    member: QFont
    title: QFont
    label: QFont

    @classmethod
    def default(cls) -> Fonts:
        def font(px: int, *, bold: bool = False, italic: bool = False) -> QFont:
            f = QFont()  # the system's sans
            f.setPixelSize(px)
            f.setBold(bold)
            f.setItalic(italic)
            return f

        return cls(
            name=font(14, bold=True),
            kind=font(11, italic=True),
            member=font(12),
            title=font(12, bold=True),
            label=font(11),
        )


_PAD_X = 12
_PAD_Y = 7
_GAP = 6  # between the header and the members


def text_size(text: str, font: QFont, pad: float = 0) -> tuple[float, float]:
    """The size of one line of text, with ``pad`` around it."""
    m = QFontMetricsF(font)
    return m.horizontalAdvance(text) + 2 * pad, m.height() + 2 * pad


class ClassItem(QGraphicsItem):
    """One class: its kind and name, and its members when they are shown."""

    def __init__(
        self, cls: Mapping[str, Any], members: Sequence[Member], fonts: Fonts
    ) -> None:
        super().__init__()
        self.cid: str = cls["id"]
        self.name: str = cls["name"]
        self.kind: str = cls["kind"]
        self.members = list(members)
        self.fonts = fonts
        self.colours = THEMES["light"]
        self.chosen = False
        self.found = False
        self.size = self.measure(cls, members, fonts)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Painted once per zoom level, then only moved: panning stays cheap.
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    @staticmethod
    def measure(
        cls: Mapping[str, Any], members: Sequence[Member], fonts: Fonts
    ) -> tuple[float, float]:
        """The box's size, in points: what Graphviz is told, and what is drawn."""
        name = QFontMetricsF(fonts.name)
        kind = QFontMetricsF(fonts.kind)
        member = QFontMetricsF(fonts.member)
        width = name.horizontalAdvance(cls["name"])
        height = name.height()
        if cls["kind"] != "class":
            width = max(width, kind.horizontalAdvance(f"«{cls['kind']}»"))
            height += kind.height()
        if members:
            width = max(width, *(member.horizontalAdvance(m.text) for m in members))
            height += _GAP + len(members) * member.height()
        return max(width + 2 * _PAD_X, 72), height + 2 * _PAD_Y

    def set_colours(self, colours: dict[str, str]) -> None:
        self.colours = colours
        self.update()

    def set_state(self, *, chosen: bool, found: bool, dimmed: bool) -> None:
        if (chosen, found) != (self.chosen, self.found):
            self.chosen, self.found = chosen, found
            self.update()
        self.setOpacity(0.35 if dimmed else 1.0)

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt's name)
        w, h = self.size
        return QRectF(-2, -2, w + 4, h + 4)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        c = self.colours
        w, h = self.size
        fill = c["panel"]
        if self.kind in _KIND_FILL:
            colour, share = _KIND_FILL[self.kind]
            painter.setBrush(mix(c[colour], share, c["panel"]))
        else:
            painter.setBrush(QColor(fill))
        if self.chosen:
            pen = QPen(QColor(c["focus"]), 3)
        elif self.found:
            pen = QPen(QColor(c["accent"]), 3)
        else:
            pen = QPen(QColor(c["ink-2"]), 1)
        painter.setPen(pen)
        painter.drawRect(QRectF(0, 0, w, h))

        ink = QColor(c["ink"])
        y = _PAD_Y
        if self.kind != "class":
            kind = QFontMetricsF(self.fonts.kind)
            painter.setFont(self.fonts.kind)
            painter.setPen(QColor(c["ink-2"]))
            painter.drawText(
                QRectF(0, y, w, kind.height()),
                Qt.AlignmentFlag.AlignCenter,
                f"«{self.kind}»",
            )
            y += kind.height()
        name = QFontMetricsF(self.fonts.name)
        painter.setFont(self.fonts.name)
        painter.setPen(ink)
        painter.drawText(
            QRectF(0, y, w, name.height()), Qt.AlignmentFlag.AlignCenter, self.name
        )
        y += name.height()
        if not self.members:
            return
        y += _GAP / 2
        painter.setPen(QPen(QColor(c["ink-2"]), 1))
        painter.drawLine(QPointF(0, y), QPointF(w, y))
        y += _GAP / 2
        member = QFontMetricsF(self.fonts.member)
        painter.setPen(ink)
        for line in self.members:
            font = QFont(self.fonts.member)
            font.setUnderline(line.static)
            painter.setFont(font)
            painter.drawText(
                QRectF(_PAD_X, y, w - _PAD_X, member.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line.text,
            )
            y += member.height()


_STYLES = {
    "inherits": Qt.PenStyle.SolidLine,
    "implements": Qt.PenStyle.DashLine,
    "holds": Qt.PenStyle.SolidLine,
    "uses": Qt.PenStyle.DotLine,
}
_ARROW = 11.0  # length of an arrowhead, in points


class EdgeItem(QGraphicsItem):
    """One arrow: a triangle at the general end for inherits and implements, else a head."""

    def __init__(
        self,
        kind: str,
        points: Sequence[tuple[float, float]],
        label: str,
        label_at: tuple[float, float] | None,
        fonts: Fonts,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.label = label
        self.fonts = fonts
        self.colours = THEMES["light"]
        p = [QPointF(x, y) for x, y in points]
        self.path = QPainterPath(p[0])
        for i in range(1, len(p) - 2, 3):
            self.path.cubicTo(p[i], p[i + 1], p[i + 2])
        # The general end is the first point: the triangle goes there.
        if kind in ("inherits", "implements"):
            self.tip, self.back = p[0], p[1]
        else:
            self.tip, self.back = p[-1], p[-2]
        self.label_rect: QRectF | None = None
        if label and label_at is not None:
            w, h = text_size(label, fonts.label, pad=2)
            self.label_rect = QRectF(label_at[0] - w / 2, label_at[1] - h / 2, w, h)
        bounds = self.path.boundingRect().adjusted(-_ARROW, -_ARROW, _ARROW, _ARROW)
        self.bounds = bounds.united(self.label_rect) if self.label_rect else bounds
        self.setZValue(-1)  # under the boxes

    def set_colours(self, colours: dict[str, str]) -> None:
        self.colours = colours
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt's name)
        return self.bounds

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        c = self.colours
        line = QColor(c["ink-2"])
        pen = QPen(line, 1.3, _STYLES[self.kind])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path)

        dx, dy = self.tip.x() - self.back.x(), self.tip.y() - self.back.y()
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length  # along the arrow, towards the tip
        base = QPointF(self.tip.x() - ux * _ARROW, self.tip.y() - uy * _ARROW)
        side = QPointF(-uy * _ARROW * 0.5, ux * _ARROW * 0.5)
        painter.setPen(QPen(line, 1.3))
        if self.kind in ("inherits", "implements"):
            painter.setBrush(QColor(c["bg"]))  # hollow: the background shows through
            painter.drawPolygon(QPolygonF([self.tip, base + side, base - side]))
        else:
            painter.drawPolyline(QPolygonF([base + side, self.tip, base - side]))

        if self.label_rect is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(c["bg"])))
            painter.drawRect(self.label_rect)
            painter.setFont(self.fonts.label)
            painter.setPen(QColor(c["ink-2"]))
            painter.drawText(self.label_rect, Qt.AlignmentFlag.AlignCenter, self.label)


class AreaItem(QGraphicsItem):
    """One area (a package): a frame around its classes, with its name."""

    def __init__(self, title: str, rect: QRectF, fonts: Fonts) -> None:
        super().__init__()
        self.title = title
        self.rect = rect
        self.fonts = fonts
        self.colours = THEMES["light"]
        self.setZValue(-2)  # under the arrows
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def set_colours(self, colours: dict[str, str]) -> None:
        self.colours = colours
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt's name)
        return self.rect.adjusted(-1, -1, 1, 1)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        c = self.colours
        painter.setPen(QPen(QColor(c["line"]), 1))
        painter.setBrush(QColor(c["panel-2"]))
        painter.drawRoundedRect(self.rect, 6, 6)
        painter.setFont(self.fonts.title)
        painter.setPen(QColor(c["ink-2"]))
        top = QFontMetricsF(self.fonts.title).height()
        painter.drawText(
            QRectF(self.rect.x() + 8, self.rect.y() + 4, self.rect.width() - 16, top),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.title,
        )
