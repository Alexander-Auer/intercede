"""The window's parts: the canvas with its floating controls, a code view, and an area's row."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QWidget,
)

from .items import ClassItem
from .style import is_dark, mono


def shadow(widget: QWidget, colours: Mapping[str, str]) -> None:
    """A soft shadow under a floating control."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(18)
    effect.setOffset(0, 3)
    colour = QColor(colours["shadow"])
    colour.setAlphaF(0.45 if is_dark(dict(colours)) else 0.12)
    effect.setColor(colour)
    widget.setGraphicsEffect(effect)


def tool(icon_name: str, tip: str, act: Callable[[], Any]) -> QToolButton:
    """A borderless button with an icon; the window gives it its icon (``icon_name``)."""
    button = QToolButton()
    button.setProperty("icon_name", icon_name)
    button.setToolTip(tip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setIconSize(QSize(18, 18))
    button.clicked.connect(act)
    return button


# -- the canvas --------------------------------------------------------------------------


class Canvas(QGraphicsView):
    """The drawing: drag to move, the wheel to zoom, a click to choose a class.

    Its controls float over it: zoom at the bottom right, the status at the bottom
    left, and, when only one class's neighbours are drawn, a note at the top.
    """

    clicked = Signal(str)

    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setOptimizationFlags(
            QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing
            | QGraphicsView.OptimizationFlag.DontSavePainterState
        )
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.press: QPoint | None = None
        self.moved = False
        self.bounds = QRectF()
        self.fit_pending = False  # asked to fit before the canvas had its size

        self.zoom_box = QFrame(self)
        self.zoom_box.setObjectName("floating")
        row = QHBoxLayout(self.zoom_box)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("zoom")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rule = QFrame()
        rule.setObjectName("vrule")
        self.tools = [
            tool("minus", "Zoom out (−)", lambda: self.zoom(1 / 1.25)),
            tool("plus", "Zoom in (+)", lambda: self.zoom(1.25)),
            tool("fit", "Fit the drawing (0)", self.fit),
        ]
        row.addWidget(self.tools[0])
        row.addWidget(self.zoom_label)
        row.addWidget(self.tools[1])
        row.addWidget(rule)
        row.addWidget(self.tools[2])

        self.status = QLabel(self)
        self.status.setObjectName("pill")
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.focus_box = QFrame(self)
        self.focus_box.setObjectName("floating")
        bar = QHBoxLayout(self.focus_box)
        bar.setContentsMargins(14, 5, 6, 5)
        bar.setSpacing(10)
        self.focus_label = QLabel()
        bar.addWidget(self.focus_label)
        self.unfocus_button = QToolButton()
        self.unfocus_button.setText("Show everything")
        self.unfocus_button.setObjectName("link")
        self.unfocus_button.setCursor(Qt.CursorShape.PointingHandCursor)
        bar.addWidget(self.unfocus_button)
        self.focus_box.hide()

    def set_colours(self, colours: Mapping[str, str]) -> None:
        for widget in (self.zoom_box, self.focus_box):
            shadow(widget, colours)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        self.place()
        if self.fit_pending and self.isVisible():
            self.fit()

    def place(self) -> None:
        """Put the floating controls in their corners."""
        margin = 16
        self.zoom_box.adjustSize()
        self.zoom_box.move(
            self.width() - margin - self.zoom_box.width(),
            self.height() - margin - self.zoom_box.height(),
        )
        self.status.adjustSize()
        self.status.move(margin, self.height() - margin - self.status.height())
        self.focus_box.adjustSize()
        self.focus_box.move((self.width() - self.focus_box.width()) // 2, margin)

    def show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.place()

    def show_focus(self, html: str | None) -> None:
        """Say that only one class and its neighbours are drawn; ``None`` hides it."""
        if html is None:
            self.focus_box.hide()
            return
        self.focus_label.setText(html)
        self.focus_box.show()
        self.place()

    def set_bounds(self, bounds: QRectF) -> None:
        """The drawing's extent; the view can move well past it, as the page's can."""
        self.bounds = bounds
        margin = max(bounds.width(), bounds.height(), 2000) * 3
        self.setSceneRect(bounds.adjusted(-margin, -margin, margin, margin))

    def scale_now(self) -> float:
        return self.transform().m11()

    def _scaled(self, k: float) -> None:
        self.setTransform(QTransform.fromScale(k, k))
        self.zoom_label.setText(f"{round(k * 100)}%")

    def zoom(self, factor: float, at: QPoint | None = None) -> None:
        k = min(max(self.scale_now() * factor, 0.05), 4.0)
        anchor = at if at is not None else self.viewport().rect().center()
        before = self.mapToScene(anchor)
        self._scaled(k)
        after = self.mapToScene(anchor)
        self.translate_by(after.x() - before.x(), after.y() - before.y())

    def translate_by(self, dx: float, dy: float) -> None:
        """Move the drawing by (dx, dy) scene points: what was at p is then at p + d."""
        centre = self.mapToScene(self.viewport().rect().center())
        self.centerOn(centre.x() - dx, centre.y() - dy)

    def fit(self) -> None:
        if self.bounds.isEmpty():
            return
        if (
            not self.isVisible()
        ):  # the window is still being built: fit once it is shown
            self.fit_pending = True
            return
        self.fit_pending = False
        view = self.viewport().rect()
        k = min(
            view.width() / self.bounds.width(),
            view.height() / self.bounds.height(),
            1.5,
        )
        self._scaled(k * 0.92)
        self.centerOn(self.bounds.center())

    def centre_on(self, rect: QRectF) -> None:
        self._scaled(max(self.scale_now(), 0.8))
        self.centerOn(rect.center())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt's name)
        if event.button() == Qt.MouseButton.LeftButton:
            self.press = event.position().toPoint()
            self.moved = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt's name)
        if self.press is not None:
            now = event.position().toPoint()
            delta = now - self.press
            if self.moved or abs(delta.x()) + abs(delta.y()) > 4:
                if not self.moved:
                    self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                self.moved = True
                h, v = self.horizontalScrollBar(), self.verticalScrollBar()
                h.setValue(h.value() - delta.x())
                v.setValue(v.value() - delta.y())
                self.press = now
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt's name)
        if event.button() == Qt.MouseButton.LeftButton and self.press is not None:
            was_drag = self.moved
            self.press = None
            self.moved = False
            self.viewport().unsetCursor()
            if not was_drag:
                item = self.itemAt(event.position().toPoint())
                while item is not None and not isinstance(item, ClassItem):
                    item = item.parentItem()
                if isinstance(item, ClassItem):
                    self.clicked.emit(item.cid)
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt's name)
        steps = event.angleDelta().y() / 120
        if steps:
            self.zoom(1.15**steps, event.position().toPoint())


# -- code, with line numbers ---------------------------------------------------------------


class PythonHighlighter(QSyntaxHighlighter):
    """Keywords, strings, comments and decorators, enough to read a class by."""

    KEYWORDS = (
        "and as assert async await break class continue def del elif else except finally "
        "for from global if import in is lambda nonlocal not or pass raise return try "
        "while with yield None True False self cls"
    ).split()

    def __init__(self, document: Any, colours: Mapping[str, str]) -> None:
        super().__init__(document)
        dark = is_dark(dict(colours))

        def fmt(
            colour: str, *, bold: bool = False, italic: bool = False
        ) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(colour))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            f.setFontItalic(italic)
            return f

        string = "#7ee787" if dark else "#1a7f37"
        self.rules = [
            (
                re.compile(r"\b(" + "|".join(self.KEYWORDS) + r")\b"),
                fmt("#ff7ab6" if dark else "#a3176b", bold=True),
            ),
            (re.compile(r"@\w+(\.\w+)*"), fmt(colours["accent"])),
            (re.compile(r"\b(?<=def |ass )\w+"), fmt(colours["accent"], bold=True)),
            (re.compile(r"(\"\"\"|''').*?\1|\"[^\"\n]*\"|'[^'\n]*'"), fmt(string)),
            (re.compile(r"#.*$"), fmt(colours["ink-3"], italic=True)),
        ]
        self.docstring = fmt(string)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt's name)
        for pattern, form in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), form)
        # Docstrings over several lines: state 1 = inside one.
        start = 0
        inside = self.previousBlockState() == 1
        while True:
            if inside:
                end = text.find('"""', start)
                if end < 0:
                    self.setFormat(start, len(text) - start, self.docstring)
                    self.setCurrentBlockState(1)
                    return
                self.setFormat(start, end + 3 - start, self.docstring)
                start, inside = end + 3, False
            else:
                begin = text.find('"""', start)
                if begin < 0 or text.count('"""', begin) % 2 == 0:
                    self.setCurrentBlockState(0)
                    return
                start, inside = begin, True


class _Gutter(QWidget):
    def __init__(self, view: CodeView) -> None:
        super().__init__(view)
        self.view = view

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt's name)
        return QSize(self.view.gutter_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt's name)
        self.view.paint_gutter(event)


class CodeView(QPlainTextEdit):
    """Read-only code with line numbers, starting at ``first``."""

    def __init__(self, text: str, colours: Mapping[str, str], first: int = 1) -> None:
        super().__init__(text)
        self.setObjectName("code")
        self.colours = colours
        self.first = first
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(mono(13))
        self.gutter = _Gutter(self)
        self.blockCountChanged.connect(lambda _: self._margin())
        self.updateRequest.connect(self._scrolled)
        self._margin()

    def gutter_width(self) -> int:
        digits = len(str(self.first + max(1, self.blockCount())))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _margin(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _scrolled(self, rect: QRect, dy: int) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        inside = self.contentsRect()
        self.gutter.setGeometry(
            QRect(inside.left(), inside.top(), self.gutter_width(), inside.height())
        )

    def paint_gutter(self, event: QPaintEvent) -> None:
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), QColor(self.colours["panel"]))
        painter.setPen(QColor(self.colours["ink-3"]))
        painter.setFont(self.font())
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(
            self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        )
        height = round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible():
                painter.drawText(
                    0,
                    top,
                    self.gutter.width() - 10,
                    height,
                    Qt.AlignmentFlag.AlignRight,
                    str(self.first + number),
                )
            block = block.next()
            top += height
            number += 1


# -- an area of the side bar ------------------------------------------------------------------


class AreaRow(QWidget):
    """An area: its check box and class count; "only" takes the count's place under the mouse."""

    def __init__(
        self,
        title: str,
        tip: str,
        count: int,
        checked: bool,
        toggled: Callable[[bool], None],
        only: Callable[[], None],
    ) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.box = QCheckBox(title)
        self.box.setToolTip(tip)
        self.box.setChecked(checked)
        self.box.toggled.connect(toggled)
        self.box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.only = QToolButton()
        self.only.setText("only")
        self.only.setObjectName("link")
        self.only.setToolTip("Show only this area")
        self.only.setCursor(Qt.CursorShape.PointingHandCursor)
        self.only.clicked.connect(only)
        badge = QLabel(str(count))
        badge.setObjectName("count")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedHeight(18)
        self.end = QStackedWidget()
        self.end.setFixedHeight(22)
        self.end.addWidget(badge)
        self.end.addWidget(self.only)
        self.end.setFixedWidth(self.only.sizeHint().width())
        row.addWidget(self.box)
        row.addWidget(self.end, 0, Qt.AlignmentFlag.AlignVCenter)

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: N802 (Qt's name)
        self.end.setCurrentIndex(1)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 (Qt's name)
        self.end.setCurrentIndex(0)
        super().leaveEvent(event)
