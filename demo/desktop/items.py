"""What the canvas draws: class boxes, arrows and areas, as Qt graphics items.

Each item paints itself from the layout (``diagram.Layout``); none of them knows the
window. Sizes are measured here before the layout, so Graphviz places boxes of
exactly the size they are drawn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontInfo,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsSceneHoverEvent,
    QStyleOptionGraphicsItem,
    QWidget,
)

from .diagram import Member
from .style import (
    LINE_STYLE,
    THEMES,
    caps,
    draw_head,
    is_dark,
    kind_colour,
    mix,
    mono,
    sans,
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
        return cls(
            name=sans(14, QFont.Weight.Bold),
            kind=caps(9.5),
            member=mono(11.5),
            title=sans(12.5, QFont.Weight.Bold),
            label=sans(10.5),
        )


_PAD_X = 14
_PAD_Y = 8
_MEMBERS_PAD = 7
_RADIUS = 7.0
_SHADOW = 3.0  # how far a box's shadow reaches below it


_WIDTHS: dict[tuple[str, str], float] = {}


def width(text: str, font: QFont) -> float:
    """How wide ``text`` is drawn: measured once per font, and by counting for code.

    Shaping text is the costly part of drawing the diagram (a tenth of a millisecond a
    line), so widths are kept; in the monospaced font of members a line's width is its
    length times one character's.
    """
    key = (font.key(), text)
    if (known := _WIDTHS.get(key)) is not None:
        return known
    metrics = QFontMetricsF(font)
    if (
        QFontInfo(font).fixedPitch() and text.isascii()
    ):  # the font loaded, not asked for
        measured = len(text) * metrics.horizontalAdvance("M")
    else:
        measured = metrics.horizontalAdvance(text)
    _WIDTHS[key] = measured
    return measured


def text_size(text: str, font: QFont, pad: float = 0) -> tuple[float, float]:
    """The size of one line of text, with ``pad`` around it."""
    return width(text, font) + 2 * pad, QFontMetricsF(font).height() + 2 * pad


class ClassItem(QGraphicsItem):
    """One class: a header with its kind and name, and its members when shown."""

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
        self.hovered = False
        self.order: int | None = None  # its place on a traced call's path
        self.size = self.measure(cls, members, fonts)
        self.header = self._header_height(cls["kind"], fonts)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptHoverEvents(True)
        # Painted once per zoom level, then only moved: panning stays cheap.
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    @staticmethod
    def _header_height(kind: str, fonts: Fonts) -> float:
        height = QFontMetricsF(fonts.name).height() + 2 * _PAD_Y
        if kind != "class":
            height += QFontMetricsF(fonts.kind).height()
        return height

    @staticmethod
    def measure(
        cls: Mapping[str, Any], members: Sequence[Member], fonts: Fonts
    ) -> tuple[float, float]:
        """The box's size, in points: what Graphviz is told, and what is drawn."""
        wide = width(cls["name"], fonts.name)
        if cls["kind"] != "class":
            wide = max(wide, width(cls["kind"].upper(), fonts.kind) + 8)
        height = ClassItem._header_height(cls["kind"], fonts)
        if members:
            wide = max(wide, *(width(m.text, fonts.member) for m in members))
            height += (
                2 * _MEMBERS_PAD + len(members) * QFontMetricsF(fonts.member).height()
            )
        return max(wide + 2 * _PAD_X, 84), height

    def set_colours(self, colours: dict[str, str]) -> None:
        self.colours = colours
        self.update()

    def set_state(self, *, chosen: bool, found: bool, dimmed: bool) -> None:
        if (chosen, found) != (self.chosen, self.found):
            self.chosen, self.found = chosen, found
            self.update()
        self.setOpacity(0.3 if dimmed else 1.0)

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802 (Qt's name)
        self.hovered = True
        self.update()

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802 (Qt's name)
        self.hovered = False
        self.update()

    def set_order(self, order: int | None) -> None:
        if order != self.order:
            self.order = order
            self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt's name)
        w, h = self.size
        return QRectF(-14, -14, w + 18, h + 14 + _SHADOW + 2)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        c = self.colours
        w, h = self.size
        dark = is_dark(c)
        box = QRectF(0, 0, w, h)
        tint = kind_colour(c, self.kind)

        # A soft shadow: two faint layers under the box, cheaper than a blur.
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, alpha in ((_SHADOW, 0.05), (_SHADOW / 2, 0.07)):
            shade = QColor(c["shadow"])
            shade.setAlphaF(alpha * (3 if dark else 1))
            painter.setBrush(shade)
            painter.drawRoundedRect(box.translated(0, spread), _RADIUS, _RADIUS)

        painter.setBrush(QColor(c["panel"]))
        painter.drawRoundedRect(box, _RADIUS, _RADIUS)

        # The header, in the colour of the kind; the body stays plain.
        header = QPainterPath()
        top = QRectF(0, 0, w, self.header)
        header.addRoundedRect(top, _RADIUS, _RADIUS)
        if self.members:
            square = QPainterPath()
            square.addRect(QRectF(0, self.header / 2, w, self.header / 2))
            header = header.united(square)
        painter.setBrush(mix(tint, 0.22 if dark else 0.12, c["panel"]))
        painter.drawPath(header)

        if self.chosen:
            border = QPen(QColor(c["focus"]), 2.5)
        elif self.found:
            border = QPen(QColor(c["accent"]), 2.2)
        elif self.hovered:
            border = QPen(QColor(c["accent"]), 1.5)
        else:
            border = QPen(mix(tint, 0.45 if dark else 0.35, c["line"]), 1.1)
        painter.setPen(border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, _RADIUS, _RADIUS)

        y = _PAD_Y
        if self.kind != "class":
            kind = QFontMetricsF(self.fonts.kind)
            painter.setFont(self.fonts.kind)
            painter.setPen(QColor(tint))
            painter.drawText(
                QRectF(0, y, w, kind.height()), Qt.AlignmentFlag.AlignCenter, self.kind
            )
            y += kind.height()
        name = QFontMetricsF(self.fonts.name)
        painter.setFont(self.fonts.name)
        painter.setPen(QColor(c["ink"]))
        painter.drawText(
            QRectF(0, y, w, name.height()), Qt.AlignmentFlag.AlignCenter, self.name
        )
        if self.order is not None:
            self._badge(painter)
        if not self.members:
            return

        y = self.header
        painter.setPen(QPen(mix(tint, 0.3, c["line"]), 1))
        painter.drawLine(QPointF(0, y), QPointF(w, y))
        y += _MEMBERS_PAD
        member = QFontMetricsF(self.fonts.member)
        for line in self.members:
            font = QFont(self.fonts.member)
            font.setUnderline(line.static)
            painter.setFont(font)
            private = line.text.startswith("-")
            painter.setPen(QColor(c["ink-3" if private else "ink-2"]))
            painter.drawText(
                QRectF(_PAD_X, y, w - _PAD_X, member.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line.text,
            )
            y += member.height()

    def _badge(self, painter: QPainter) -> None:
        """Its place on a traced call's path: a numbered dot at the top left."""
        c = self.colours
        r = 11.0
        centre = QPointF(0, 0)
        painter.setPen(QPen(QColor(c["panel"]), 2))
        painter.setBrush(QColor(c["focus"]))
        painter.drawEllipse(centre, r, r)
        font = QFont(self.fonts.name)
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(
            QRectF(-r, -r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, str(self.order)
        )


class EdgeItem(QGraphicsItem):
    """One arrow: a triangle at the general end for inherits and implements, else a head."""

    def __init__(
        self,
        relation: Mapping[str, Any],
        points: Sequence[tuple[float, float]],
        label_at: tuple[float, float] | None,
        fonts: Fonts,
    ) -> None:
        super().__init__()
        self.kind: str = relation["kind"]
        self.ends = (relation["from"], relation["to"])
        self.label: str = relation.get("label", "")
        self.fonts = fonts
        self.colours = THEMES["light"]
        self.lit = False  # one end is the chosen class
        p = [QPointF(x, y) for x, y in points]
        self.path = QPainterPath(p[0])
        for i in range(1, len(p) - 2, 3):
            self.path.cubicTo(p[i], p[i + 1], p[i + 2])
        # The general end is the first point: the triangle goes there.
        if self.kind in ("inherits", "implements"):
            self.tip, self.back = p[0], p[1]
        else:
            self.tip, self.back = p[-1], p[-2]
        self.label_rect: QRectF | None = None
        if self.label and label_at is not None:
            w, h = text_size(self.label, fonts.label, pad=3)
            w += 6
            self.label_rect = QRectF(label_at[0] - w / 2, label_at[1] - h / 2, w, h)
        margin = 12
        bounds = self.path.boundingRect().adjusted(-margin, -margin, margin, margin)
        self.bounds = bounds.united(self.label_rect) if self.label_rect else bounds
        self.setZValue(-1)  # under the boxes

    def set_colours(self, colours: dict[str, str]) -> None:
        self.colours = colours
        self.update()

    def set_state(self, *, lit: bool, faded: bool) -> None:
        """Lit: an arrow of the chosen class, drawn over the others. Faded: one of the rest."""
        if lit != self.lit:
            self.lit = lit
            self.setZValue(-0.5 if lit else -1)
            self.update()
        self.setOpacity(0.18 if faded else 1.0)

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt's name)
        return self.bounds

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        c = self.colours
        if self.lit:
            line, width = QColor(c["accent"]), 2.0
        else:
            line = mix(c["ink-2"], 0.75, c["bg"])
            width = 1.2
        pen = QPen(line, width, LINE_STYLE[self.kind])
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path)
        painter.setPen(QPen(line, width))
        draw_head(painter, self.kind, self.tip, self.back, line, QColor(c["panel"]))

        if self.label_rect is not None:
            painter.setPen(QPen(QColor(c["accent"] if self.lit else c["line"]), 1))
            painter.setBrush(QColor(c["panel"]))
            radius = self.label_rect.height() / 2
            painter.drawRoundedRect(self.label_rect, radius, radius)
            painter.setFont(self.fonts.label)
            painter.setPen(QColor(c["accent"] if self.lit else c["ink-2"]))
            painter.drawText(self.label_rect, Qt.AlignmentFlag.AlignCenter, self.label)


class AreaItem(QGraphicsItem):
    """One area (a package): a soft frame around its classes, with its name."""

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
        painter.setPen(QPen(QColor(c["line-2"]), 1))
        painter.setBrush(mix(c["panel-2"], 0.55, c["bg"]))
        painter.drawRoundedRect(self.rect, 12, 12)
        painter.setFont(self.fonts.title)
        painter.setPen(QColor(c["ink-3"]))
        top = QFontMetricsF(self.fonts.title).height()
        painter.drawText(
            QRectF(self.rect.x() + 12, self.rect.y() + 7, self.rect.width() - 24, top),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.title,
        )
