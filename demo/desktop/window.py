"""The class diagram window: the web page's diagram (/classes) as a desktop app.

Everything the page does is here: areas and switches, search, a class's panel with
its relations, its neighbours only, its source, pan and zoom, and the Mermaid text.
Two things are new, both to keep it quick and current:

- **Laid out by Graphviz, drawn by Qt.** ``dot`` places the diagram in a tenth of a
  second, in its own process, so the window never waits; each layout is kept, in
  memory and on disk, so a view seen before is drawn at once, also after a restart.
- **Read from the checkout, again when it changes.** The model is read by the
  checkout's own Python (``extract.py``), again whenever a file under
  ``src/intercede`` changes or another branch is checked out. The last model read is
  kept, so the window opens with it while the code is read again.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QFileSystemWatcher,
    QObject,
    QPoint,
    QProcess,
    QProcessEnvironment,
    QRectF,
    QSettings,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QResizeEvent,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import diagram as d
from .items import THEMES, AreaItem, ClassItem, EdgeItem, Fonts, text_size

HERE = Path(__file__).resolve().parent


def classes_py() -> Path:
    """The ``classes.py`` that reads the model: installed next to the app, or demo/'s."""
    installed = HERE / "classes.py"
    return installed if installed.exists() else HERE.parent / "classes.py"


def icon_svg() -> Path:
    """The app's icon: installed next to the app, or the demo's."""
    installed = HERE / "icon.svg"
    return installed if installed.exists() else HERE.parent / "static" / "icon.svg"


def checkout_python(repo: Path) -> list[str]:
    """The command that runs the checkout's Python, which has ``intercede`` installed."""
    for env in ("py313", "demo", "py312", "py314", "py311"):
        python = repo / ".pixi" / "envs" / env / "bin" / "python"
        if python.exists():
            return [str(python)]
    return [
        "pixi",
        "run",
        "--manifest-path",
        str(repo / "pyproject.toml"),
        "-e",
        "py313",
        "python",
    ]


def branch(repo: Path) -> str:
    """The branch checked out, or the commit for a detached head."""
    try:
        head = (repo / ".git" / "HEAD").read_text().strip()
    except OSError:
        return ""
    return (
        head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:10]
    )


def cache_dir() -> Path:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.CacheLocation
    )
    path = Path(base or Path.home() / ".cache" / "intercede-classes")
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- reading the code, and laying it out, in processes of their own ------------------------


class Reader(QObject):
    """Reads the model with the checkout's Python, in a process of its own."""

    loaded = Signal(dict)
    failed = Signal(str)

    def __init__(self, repo: Path, python: list[str]) -> None:
        super().__init__()
        self.repo = repo
        self.python = python
        self.process: QProcess | None = None
        self.again = False  # the code changed while it was being read

    def read(self) -> None:
        if self.process is not None:
            self.again = True
            return
        process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        for name in ("PYTHONPATH", "PYTHONHOME"):  # the app's, not the checkout's
            environment.remove(name)
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(self.repo))
        process.finished.connect(lambda *_: self._done(process))
        self.process = process
        command = [*self.python, str(HERE / "extract.py"), str(classes_py())]
        process.start(command[0], command[1:])

    def stop(self) -> None:
        """Stop a read still running: the app is closing."""
        if self.process is not None:
            self.process.finished.disconnect()
            self.process.kill()
            self.process.waitForFinished(1000)
            self.process.deleteLater()
            self.process = None
        self.again = False

    def _done(self, process: QProcess) -> None:
        self.process = None
        out = bytes(process.readAllStandardOutput().data())
        if process.exitCode() == 0 and out:
            try:
                self.loaded.emit(json.loads(out))
            except json.JSONDecodeError as exc:
                self.failed.emit(f"the code could not be read: {exc}")
        else:
            error = bytes(process.readAllStandardError().data()).decode(
                errors="replace"
            )
            lines = [line for line in error.splitlines() if line.strip()]
            self.failed.emit(lines[-1] if lines else "the code could not be read")
        process.deleteLater()
        if self.again:
            self.again = False
            self.read()


class Layouter(QObject):
    """Lays out with Graphviz, in a process of its own; each layout is kept, keyed by its input."""

    done = Signal(int, object)  # request number, Layout
    failed = Signal(int, str)

    def __init__(self, dot: str) -> None:
        super().__init__()
        self.dot = dot
        self.kept: dict[str, str] = {}
        self.folder = cache_dir() / "layouts"
        self.folder.mkdir(exist_ok=True)
        self.process: QProcess | None = None

    def cached(self, text: str) -> str | None:
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self.kept:
            return self.kept[key]
        try:
            out = (self.folder / f"{key}.json").read_text()
        except OSError:
            return None
        self.kept[key] = out
        return out

    def request(self, number: int, text: str, relations: list[Any]) -> bool:
        """Lay out ``text``; True if it was kept, and ``done`` has been emitted already."""
        if (out := self.cached(text)) is not None:
            self.done.emit(number, d.parse_layout(out, relations))
            return True
        self.stop()  # a newer view replaces one still being laid out
        process = QProcess(self)
        process.finished.connect(
            lambda *_: self._done(process, number, text, relations)
        )
        self.process = process
        process.start(self.dot, ["-Tjson"])
        process.write(text.encode())
        process.closeWriteChannel()
        return False

    def stop(self) -> None:
        """Stop a layout still running: a newer view replaces it, or the app is closing."""
        if self.process is not None:
            self.process.finished.disconnect()
            self.process.kill()
            self.process.waitForFinished(1000)
            self.process.deleteLater()
            self.process = None

    def _done(
        self, process: QProcess, number: int, text: str, relations: list[Any]
    ) -> None:
        if self.process is process:
            self.process = None
        out = bytes(process.readAllStandardOutput().data()).decode()
        process.deleteLater()
        if process.exitCode() != 0 or not out:
            error = bytes(process.readAllStandardError().data()).decode(
                errors="replace"
            )
            self.failed.emit(number, error.strip() or "Graphviz could not lay this out")
            return
        key = hashlib.sha256(text.encode()).hexdigest()
        self.kept[key] = out
        try:
            (self.folder / f"{key}.json").write_text(out)
        except OSError:
            pass  # kept for this run only
        self.done.emit(number, d.parse_layout(out, relations))


# -- the canvas --------------------------------------------------------------------------


class Canvas(QGraphicsView):
    """The drawing: drag to move, the wheel to zoom, a click to choose a class."""

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

        self.zoom_in = self._overlay("+", "Zoom in (+)", lambda: self.zoom(1.25))
        self.zoom_out = self._overlay("−", "Zoom out (−)", lambda: self.zoom(1 / 1.25))
        self.zoom_fit = self._overlay("Fit", "Fit (0)", self.fit)
        self.status = QLabel(self)
        self.status.setObjectName("status")
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def _overlay(self, text: str, tip: str, act: Callable[[], None]) -> QPushButton:
        button = QPushButton(text, self)
        button.setToolTip(tip)
        button.setMinimumWidth(40)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(act)
        return button

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt's name)
        super().resizeEvent(event)
        right = self.width() - 14
        for button in (self.zoom_fit, self.zoom_out, self.zoom_in):
            button.adjustSize()
            right -= button.width()
            button.move(right, self.height() - 14 - button.height())
            right -= 6
        self.place_status()

    def place_status(self) -> None:
        self.status.adjustSize()
        self.status.move(14, self.height() - 14 - self.status.height())

    def show_status(self, text: str) -> None:
        self.status.setText(text)
        self.place_status()

    def set_bounds(self, bounds: QRectF) -> None:
        """The drawing's extent; the view can move well past it, as the page's can."""
        self.bounds = bounds
        margin = max(bounds.width(), bounds.height(), 2000) * 3
        self.setSceneRect(bounds.adjusted(-margin, -margin, margin, margin))

    def scale_now(self) -> float:
        return self.transform().m11()

    def zoom(self, factor: float, at: QPoint | None = None) -> None:
        k = min(max(self.scale_now() * factor, 0.05), 4.0)
        anchor = at if at is not None else self.viewport().rect().center()
        before = self.mapToScene(anchor)
        self.setTransform(QTransform.fromScale(k, k))
        after = self.mapToScene(anchor)
        self.translate_by(after.x() - before.x(), after.y() - before.y())

    def translate_by(self, dx: float, dy: float) -> None:
        """Move the drawing by (dx, dy) scene points: what was at p is then at p + d."""
        centre = self.mapToScene(self.viewport().rect().center())
        self.centerOn(centre.x() - dx, centre.y() - dy)

    def fit(self) -> None:
        if self.bounds.isEmpty():
            return
        view = self.viewport().rect()
        k = min(
            view.width() / self.bounds.width(),
            view.height() / self.bounds.height(),
            1.5,
        )
        k *= 0.95
        self.setTransform(QTransform.fromScale(k, k))
        self.centerOn(self.bounds.center())

    def centre_on(self, rect: QRectF) -> None:
        k = max(self.scale_now(), 0.8)
        self.setTransform(QTransform.fromScale(k, k))
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


# -- the source of a class ---------------------------------------------------------------


class PythonHighlighter(QSyntaxHighlighter):
    """Keywords, strings, comments and decorators, enough to read a class by."""

    KEYWORDS = (
        "and as assert async await break class continue def del elif else except finally "
        "for from global if import in is lambda nonlocal not or pass raise return try "
        "while with yield None True False self cls"
    ).split()

    def __init__(self, document: Any, colours: Mapping[str, str]) -> None:
        super().__init__(document)
        dark = QColor(colours["bg"]).lightness() < 128

        def fmt(
            colour: str, *, bold: bool = False, italic: bool = False
        ) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(colour))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            f.setFontItalic(italic)
            return f

        self.rules = [
            (
                re.compile(r"\b(" + "|".join(self.KEYWORDS) + r")\b"),
                fmt("#ff7ab6" if dark else "#a3176b", bold=True),
            ),
            (re.compile(r"@\w+(\.\w+)*"), fmt(colours["accent"])),
            (re.compile(r"\b(def|class)\s+(\w+)"), fmt(colours["accent"], bold=True)),
            (
                re.compile(r"(\"\"\"|''').*?\1|\"[^\"\n]*\"|'[^'\n]*'"),
                fmt("#7ee787" if dark else "#1a7f37"),
            ),
            (re.compile(r"#.*$"), fmt(colours["ink-2"], italic=True)),
        ]
        self.docstring = fmt("#7ee787" if dark else "#1a7f37")

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


class SourceDialog(QDialog):
    """A class's source, or the Mermaid text of what is drawn."""

    def __init__(
        self,
        parent: QWidget,
        title: str,
        text: str,
        colours: Mapping[str, str],
        *,
        python: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(960, 720)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("heading")
        heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(heading)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        if python:
            self.highlighter = PythonHighlighter(view.document(), colours)
        layout.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)


# -- the window --------------------------------------------------------------------------

HEADINGS = [
    ("inherits", "from", "Inherits from"),
    ("inherits", "to", "Inherited by"),
    ("implements", "from", "Implements"),
    ("implements", "to", "Implemented by"),
    ("holds", "from", "Holds"),
    ("holds", "to", "Held by"),
    ("uses", "from", "Uses"),
    ("uses", "to", "Used by"),
]


def _link_button(text: str, tip: str = "") -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tip)
    button.setAutoRaise(True)
    button.setObjectName("link")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class Window(QMainWindow):
    """The class diagram: a side bar of filters, the canvas, and a class's panel."""

    def __init__(
        self, repo: Path, python: list[str], dot: str, open_class: str | None
    ) -> None:
        super().__init__()
        self.repo = repo
        self.stored = QSettings()
        self.settings = d.Settings(
            members=self.stored.value("members", False, type=bool),
            private=self.stored.value("private", False, type=bool),
            uses=self.stored.value("uses", False, type=bool),
            exceptions=self.stored.value("exceptions", True, type=bool),
            hidden=set(self.stored.value("hidden", [], type=list) or []),
        )
        self.theme_choice = str(self.stored.value("theme", "system"))
        self.colours = THEMES["light"]
        self.fonts = Fonts.default()
        self.model: dict[str, Any] | None = None
        self.sources: dict[str, str] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        self.read_at = ""
        self.focus: str | None = None
        self.chosen: str | None = None
        self.text = ""  # the Mermaid text of what is drawn
        self.shown: tuple[list[Any], list[Any]] = ([], [])
        self.items: dict[str, ClassItem] = {}
        self.request = 0
        self.then: list[Callable[[], None]] = []  # to do once the next layout is drawn
        self.fit_next = False
        self.open_class = open_class
        self.model_file = (
            cache_dir()
            / f"model-{hashlib.sha256(str(repo).encode()).hexdigest()[:12]}.json"
        )

        self.setWindowTitle(f"interCEde classes — {repo.name}")
        self.scene = QGraphicsScene(self)
        self.scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self.canvas = Canvas(self.scene)
        self.canvas.clicked.connect(self.choose)

        self.reader = Reader(repo, python)
        self.reader.loaded.connect(self.loaded)
        self.reader.failed.connect(self.read_failed)
        self.layouter = Layouter(dot)
        self.layouter.done.connect(self.laid_out)
        self.layouter.failed.connect(self.layout_failed)

        self.build()
        self.apply_theme()
        self.watch()
        QApplication.instance().aboutToQuit.connect(self.stop)  # type: ignore[union-attr]
        geometry = self.stored.value("geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        else:
            self.resize(1500, 950)

        # The last model read opens the window; the code is read again behind it.
        try:
            cached = json.loads(self.model_file.read_text())
        except (OSError, json.JSONDecodeError):
            cached = None
        if cached is not None:
            self.use(cached, read_at=cached.get("read_at", "earlier"), fit=True)
            if self.items:  # drawn from a kept layout already
                self.canvas.show_status(
                    f"{self.canvas.status.text()} · checking the code for changes…"
                )
        else:
            self.canvas.show_status("Reading the code…")
        self.reader.read()

    # -- building the window ------------------------------------------------------

    def build(self) -> None:
        side = QWidget()
        side.setObjectName("side")
        column = QVBoxLayout(side)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(6)

        self.caption = QLabel()
        self.caption.setWordWrap(True)
        self.caption.setObjectName("muted")
        column.addWidget(self.caption)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a class…")
        self.search.setClearButtonEnabled(True)
        self.completer = QCompleter([])
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.activated.connect(self.go_name)
        self.search.setCompleter(self.completer)
        self.search.textEdited.connect(self.searched)
        self.search.returnPressed.connect(self.search_enter)
        column.addWidget(self.search)

        column.addWidget(self._heading("Show"))
        self.switches: dict[str, QCheckBox] = {}
        for key, text in [
            ("members", "fields and methods"),
            ("private", "private members"),
            ("uses", "“uses” arrows"),
            ("exceptions", "exceptions"),
        ]:
            box = QCheckBox(text)
            box.setChecked(getattr(self.settings, key))
            box.toggled.connect(lambda on, key=key: self.switch(key, on))
            self.switches[key] = box
            column.addWidget(box)

        row = QHBoxLayout()
        row.addWidget(self._heading("Areas"))
        every = _link_button("all", "Show every area")
        every.clicked.connect(lambda: self.set_all(True))
        none = _link_button("none", "Hide every area")
        none.clicked.connect(lambda: self.set_all(False))
        row.addWidget(every)
        row.addWidget(none)
        row.addStretch()
        column.addLayout(row)
        self.areas_box = QWidget()
        self.areas_grid = QGridLayout(self.areas_box)
        self.areas_grid.setContentsMargins(0, 0, 0, 0)
        self.areas_grid.setHorizontalSpacing(4)
        self.areas_grid.setVerticalSpacing(2)
        self.area_boxes: dict[str, QCheckBox] = {}
        column.addWidget(self.areas_box)

        column.addWidget(self._heading("Arrows"))
        legend = QLabel(
            "<table cellspacing=3>"
            "<tr><td><tt>──▷</tt></td><td>inherits from</td></tr>"
            "<tr><td><tt>┄┄▷</tt></td><td>implements the protocol</td></tr>"
            "<tr><td><tt>──&gt;</tt></td><td>holds, as a field</td></tr>"
            "<tr><td><tt>┄┄&gt;</tt></td><td>uses, in a method</td></tr></table>"
        )
        column.addWidget(legend)

        column.addWidget(self._heading("Mermaid"))
        row = QHBoxLayout()
        for text, act in [
            ("Copy", self.copy_text),
            ("Save .mmd…", self.save_text),
            ("View", self.view_text),
        ]:
            button = QPushButton(text)
            button.clicked.connect(act)
            row.addWidget(button)
        self.copy_button = row.itemAt(0).widget()
        column.addLayout(row)

        column.addWidget(self._heading("App"))
        row = QHBoxLayout()
        reload_button = QPushButton("Read the code again")
        reload_button.setToolTip(
            "Read the code again (F5); it is also read when a file changes"
        )
        reload_button.clicked.connect(self.reader.read)
        row.addWidget(reload_button)
        self.theme_box = QComboBox()
        self.theme_box.addItems(["system", "light", "dark"])
        self.theme_box.setCurrentText(self.theme_choice)
        self.theme_box.currentTextChanged.connect(self.set_theme)
        row.addWidget(self.theme_box)
        column.addLayout(row)
        column.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(300)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        centre = QWidget()
        stack = QVBoxLayout(centre)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        self.focus_bar = QFrame()
        self.focus_bar.setObjectName("focusbar")
        bar = QHBoxLayout(self.focus_bar)
        bar.setContentsMargins(12, 6, 12, 6)
        self.focus_label = QLabel()
        bar.addWidget(self.focus_label)
        bar.addStretch()
        unfocus = QPushButton("Show everything")
        unfocus.clicked.connect(self.unfocus)
        bar.addWidget(unfocus)
        self.focus_bar.hide()
        stack.addWidget(self.focus_bar)
        stack.addWidget(self.canvas)

        self.detail = self._detail()
        self.detail.hide()

        self.splitter = QSplitter()
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(centre)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 900, 400])
        self.setCentralWidget(self.splitter)

        for keys, act in [
            (["+", "="], lambda: self.canvas.zoom(1.25)),
            (["-"], lambda: self.canvas.zoom(1 / 1.25)),
            (["0"], self.canvas.fit),
            (["Escape"], lambda: self.choose(None)),
            (["Ctrl+F"], self.search.setFocus),
            (["F5", "Ctrl+R"], self.reader.read),
        ]:
            for key in keys:
                QShortcut(QKeySequence(key), self, act)

    def _heading(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("heading")
        return label

    def _detail(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("detail")
        panel.setMinimumWidth(340)
        column = QVBoxLayout(panel)
        column.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout()
        self.detail_name = QLabel()
        self.detail_name.setObjectName("title")
        self.detail_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        top.addWidget(self.detail_name, 1)
        close = _link_button("✕", "Close (Esc)")
        close.clicked.connect(lambda: self.choose(None))
        top.addWidget(close)
        column.addLayout(top)
        self.detail_kind = QLabel()
        self.detail_kind.setObjectName("chip")
        row = QHBoxLayout()
        row.addWidget(self.detail_kind)
        row.addStretch()
        column.addLayout(row)
        self.detail_where = QLabel()
        self.detail_where.setObjectName("muted")
        self.detail_where.setWordWrap(True)
        self.detail_where.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        column.addWidget(self.detail_where)
        actions = QGridLayout()
        self.focus_button = QPushButton()
        self.focus_button.clicked.connect(self.toggle_focus)
        source = QPushButton("Source")
        source.clicked.connect(lambda: self.show_source(self.chosen))
        centre = QPushButton("Centre")
        centre.clicked.connect(lambda: self.centre(self.chosen))
        editor = QPushButton("Open")
        editor.setToolTip("Open the class in the editor")
        editor.clicked.connect(lambda: self.open_in_editor(self.chosen))
        actions.addWidget(self.focus_button, 0, 0, 1, 3)
        for place, button in enumerate((source, centre, editor)):
            actions.addWidget(button, 1, place)
        column.addLayout(actions)
        self.detail_text = QTextBrowser()
        self.detail_text.setOpenLinks(False)
        self.detail_text.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_text.anchorClicked.connect(lambda url: self.go(url.path()))
        column.addWidget(self.detail_text, 1)
        return panel

    # -- theme ----------------------------------------------------------------------

    def set_theme(self, choice: str) -> None:
        self.theme_choice = choice
        self.stored.setValue("theme", choice)
        self.apply_theme()

    def apply_theme(self) -> None:
        choice = self.theme_choice
        if choice == "system":
            scheme = QGuiApplication.styleHints().colorScheme()
            choice = "dark" if scheme == Qt.ColorScheme.Dark else "light"
        c = self.colours = THEMES[choice]
        palette = QPalette()
        roles = {
            QPalette.ColorRole.Window: c["panel"],
            QPalette.ColorRole.WindowText: c["ink"],
            QPalette.ColorRole.Base: c["bg"],
            QPalette.ColorRole.AlternateBase: c["panel-2"],
            QPalette.ColorRole.Text: c["ink"],
            QPalette.ColorRole.Button: c["panel-2"],
            QPalette.ColorRole.ButtonText: c["ink"],
            QPalette.ColorRole.Highlight: c["accent"],
            QPalette.ColorRole.HighlightedText: "#ffffff",
            QPalette.ColorRole.ToolTipBase: c["panel"],
            QPalette.ColorRole.ToolTipText: c["ink"],
            QPalette.ColorRole.PlaceholderText: c["ink-2"],
            QPalette.ColorRole.Link: c["accent"],
            # The frames of check boxes and fields: visible on either background.
            QPalette.ColorRole.Light: c["panel"],
            QPalette.ColorRole.Midlight: c["panel-2"],
            QPalette.ColorRole.Mid: c["line"],
            QPalette.ColorRole.Dark: c["ink-2"],
            QPalette.ColorRole.Shadow: c["line"],
        }
        for role, colour in roles.items():
            palette.setColor(role, QColor(colour))
        QApplication.setPalette(palette)
        self.setStyleSheet(
            f"""
            QLabel#heading {{ color: {c["ink-2"]}; font-weight: bold; font-size: 11px;
                              letter-spacing: 1px; margin-top: 10px; }}
            QLabel#muted, QLabel#status {{ color: {c["ink-2"]}; }}
            QLabel#status {{ background: {c["bg"]}; padding: 2px 8px; border-radius: 6px; }}
            QLabel#title {{ font-size: 20px; font-weight: bold; }}
            QLabel#chip {{ background: {c["panel-2"]}; border: 1px solid {c["line"]};
                           border-radius: 9px; padding: 1px 9px; }}
            QToolButton#link {{ color: {c["accent"]}; border: none; }}
            QFrame#focusbar {{ background: {c["panel"]}; border-bottom: 1px solid {c["line"]}; }}
            QWidget#detail {{ background: {c["panel"]}; }}
            QTextBrowser {{ background: {c["panel"]}; }}
            QGraphicsView {{ background: {c["bg"]}; }}
            """
        )
        self.detail_text.document().setDefaultStyleSheet(
            f"""
            h3 {{ color: {c["ink-2"]}; font-size: 12px; margin-top: 14px; }}
            a {{ color: {c["accent"]}; text-decoration: none; }}
            .sig {{ font-family: monospace; }}
            .private {{ color: {c["ink-2"]}; }}
            small {{ color: {c["ink-2"]}; }}
            """
        )
        for item in self.scene.items():
            if isinstance(item, ClassItem | EdgeItem | AreaItem):
                item.set_colours(self.colours)
        self.canvas.setBackgroundBrush(QColor(c["bg"]))
        if self.chosen:
            self.choose(self.chosen)

    # -- the model ------------------------------------------------------------------

    def watch(self) -> None:
        """Read the code again when a file of the package changes, or the branch does."""
        self.watcher = QFileSystemWatcher(self)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(400)  # an editor or git writes several files at once
        self.debounce.timeout.connect(self.changed)
        self.watcher.fileChanged.connect(lambda _: self.debounce.start())
        self.watcher.directoryChanged.connect(lambda _: self.debounce.start())
        self.add_watches()

    def add_watches(self) -> None:
        paths = []
        head = self.repo / ".git" / "HEAD"
        if head.exists():
            paths.append(str(head))
        for root, dirs, files in os.walk(self.repo / "src" / "intercede"):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            paths.append(root)
            paths += [os.path.join(root, f) for f in files if f.endswith(".py")]
        watched = set(self.watcher.files()) | set(self.watcher.directories())
        new = [p for p in paths if p not in watched]
        if new:
            self.watcher.addPaths(new)

    def changed(self) -> None:
        self.add_watches()  # a file saved by renaming, or a new one, is watched afresh
        self.canvas.show_status("The code changed: reading it again…")
        self.reader.read()

    def loaded(self, data: dict[str, Any]) -> None:
        data["read_at"] = time.strftime("%H:%M:%S")
        old = self.model
        if old is not None and data["model"] == old and data["sources"] == self.sources:
            self.read_at = data["read_at"]
            self.update_caption()
            self.show_counts()
            return
        try:
            self.model_file.write_text(json.dumps(data))
        except OSError:
            pass
        self.use(data, read_at=data["read_at"], fit=old is None)

    def read_failed(self, reason: str) -> None:
        shown = (
            "the last diagram read is shown" if self.model else "nothing to show yet"
        )
        self.canvas.show_status(f"Reading the code failed: {reason} ({shown})")

    def use(self, data: dict[str, Any], *, read_at: str, fit: bool) -> None:
        """Show a model; a chosen or focused class stays, found again by its module and name."""

        def name_of(cid: str | None) -> tuple[str, str] | None:
            c = self.by_id.get(cid or "")
            return (c["module"], c["name"]) if c else None

        chosen, focus = name_of(self.chosen), name_of(self.focus)
        self.model = data["model"]
        self.sources = data.get("sources", {})
        self.by_id = {c["id"]: c for c in self.model["classes"]}
        by_name = {(c["module"], c["name"]): c["id"] for c in self.model["classes"]}
        self.chosen = by_name.get(chosen) if chosen else None
        self.focus = by_name.get(focus) if focus else None
        self.read_at = read_at
        areas = {a["name"] for a in self.model["areas"]}
        self.settings.hidden &= areas
        self.build_areas()
        self.completer.model().setStringList(
            sorted({c["name"] for c in self.model["classes"]})
        )  # type: ignore[attr-defined]
        self.update_caption()
        self.show_focus()

        then: list[Callable[[], None]] = []
        if self.chosen:
            then.append(lambda: self.choose(self.chosen))
        elif self.detail.isVisible():
            then.append(lambda: self.choose(None))
        if self.open_class:
            wanted, self.open_class = self.open_class, None
            match = next(
                (c for c in self.model["classes"] if c["name"] == wanted), None
            )
            if match is not None:
                then.append(lambda: self.go(match["id"]))
        self.render(fit=fit, then=then)

    def update_caption(self) -> None:
        if not self.model:
            return
        where = branch(self.repo)
        self.caption.setText(
            f"Every class in <b>{self.model['package']}</b>: {len(self.model['classes'])} "
            f"classes in {len(self.model['areas'])} areas, read from the code"
            f"{f' on <b>{html.escape(where)}</b>' if where else ''} at {self.read_at}."
        )

    def build_areas(self) -> None:
        assert self.model is not None
        while self.areas_grid.count():
            widget = self.areas_grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.area_boxes = {}
        counts: dict[str, int] = {}
        for c in self.model["classes"]:
            counts[c["area"]] = counts.get(c["area"], 0) + 1
        for row, area in enumerate(self.model["areas"]):
            name = area["name"]
            box = QCheckBox(d.short(self.model, name))
            box.setToolTip(area["doc"])
            box.setChecked(name not in self.settings.hidden)
            box.toggled.connect(lambda on, name=name: self.toggle_area(name, on))
            only = _link_button("only", "Show only this area")
            only.clicked.connect(lambda _=False, name=name: self.only_area(name))
            count = QLabel(str(counts.get(name, 0)))
            count.setObjectName("muted")
            self.areas_grid.addWidget(box, row, 0)
            self.areas_grid.addWidget(only, row, 1)
            self.areas_grid.addWidget(count, row, 2, Qt.AlignmentFlag.AlignRight)
            self.area_boxes[name] = box
        self.areas_grid.setColumnStretch(0, 1)
        self.areas_grid.setColumnMinimumWidth(2, 22)

    # -- what is drawn ----------------------------------------------------------------

    def save_settings(self) -> None:
        for key in ("members", "private", "uses", "exceptions"):
            self.stored.setValue(key, getattr(self.settings, key))
        self.stored.setValue("hidden", sorted(self.settings.hidden))

    def switch(self, key: str, on: bool) -> None:
        setattr(self.settings, key, on)
        self.save_settings()
        self.render(fit=True)

    def toggle_area(self, area: str, on: bool) -> None:
        if on:
            self.settings.hidden.discard(area)
        else:
            self.settings.hidden.add(area)
        self.save_settings()
        self.render(fit=True)

    def set_boxes(self) -> None:
        for name, box in self.area_boxes.items():
            box.blockSignals(True)
            box.setChecked(name not in self.settings.hidden)
            box.blockSignals(False)

    def only_area(self, area: str) -> None:
        assert self.model is not None
        self.settings.hidden = {a["name"] for a in self.model["areas"]} - {area}
        self.set_boxes()
        self.focus = None
        self.show_focus()
        self.save_settings()
        self.render(fit=True)

    def set_all(self, on: bool) -> None:
        assert self.model is not None
        self.settings.hidden = set() if on else {a["name"] for a in self.model["areas"]}
        self.set_boxes()
        self.save_settings()
        self.render(fit=True)

    def render(
        self, *, fit: bool = False, then: list[Callable[[], None]] | None = None
    ) -> None:
        """Lay out and draw what the filters let through; ``then`` runs once it is drawn."""
        if self.model is None:
            return
        model = self.model
        classes = d.shown_classes(model, self.settings, self.focus)
        relations = d.shown_relations(model, {c["id"] for c in classes}, self.settings)
        self.text = d.mermaid_text(model, classes, relations, self.settings)
        self.shown = (classes, relations)
        self.then = list(then or [])
        self.fit_next = fit
        self.request += 1
        if not classes:
            self.scene.clear()
            self.items = {}
            self.canvas.show_status("Nothing to draw: switch an area on.")
            return
        sizes = {
            c["id"]: ClassItem.measure(c, d.members(c, self.settings), self.fonts)
            for c in classes
        }
        texts = {}
        for c in classes:
            title = d.short(model, c["area"])
            w, h = text_size(title, self.fonts.title, pad=4)
            texts[title] = (w + 12, h + 4)
        for r in relations:
            if r.get("label"):
                texts[r["label"]] = text_size(r["label"], self.fonts.label, pad=3)
        dot = d.dot_text(model, classes, relations, sizes, texts)
        if not self.layouter.request(self.request, dot, relations):
            self.canvas.show_status("Laying out…")

    def laid_out(self, number: int, layout: d.Layout) -> None:
        if number != self.request or self.model is None:
            return  # a newer view was asked for meanwhile
        classes, _ = self.shown
        self.scene.clear()
        self.items = {}
        for area, box in layout.areas.items():
            item = AreaItem(
                d.short(self.model, area),
                QRectF(box.x, box.y, box.w, box.h),
                self.fonts,
            )
            item.set_colours(self.colours)
            self.scene.addItem(item)
        for edge in layout.edges:
            item = EdgeItem(
                edge.relation["kind"],
                edge.points,
                edge.relation.get("label", ""),
                edge.label_at,
                self.fonts,
            )
            item.set_colours(self.colours)
            self.scene.addItem(item)
        for c in classes:
            box = layout.nodes[c["id"]]
            node = ClassItem(c, d.members(c, self.settings), self.fonts)
            node.set_colours(self.colours)
            node.setPos(box.x, box.y)
            self.scene.addItem(node)
            self.items[c["id"]] = node
        self.canvas.set_bounds(QRectF(0, 0, layout.width, layout.height))
        self.mark()
        self.show_counts()
        if self.fit_next:
            self.canvas.fit()
        then, self.then = self.then, []
        for act in then:
            act()

    def layout_failed(self, number: int, reason: str) -> None:
        if number == self.request:
            self.canvas.show_status(f"Graphviz could not lay this out: {reason}")

    def show_counts(self) -> None:
        classes, relations = self.shown
        if classes:
            self.canvas.show_status(
                f"{len(classes)} classes, {len(relations)} arrows · drag to move, "
                "wheel to zoom, click a class"
            )

    def mark(self) -> None:
        query = self.search.text().strip().lower()
        for cid, item in self.items.items():
            found = bool(query) and query in item.name.lower()
            item.set_state(
                chosen=cid == self.chosen, found=found, dimmed=bool(query) and not found
            )

    # -- search -----------------------------------------------------------------------

    def searched(self, text: str) -> None:
        self.mark()
        assert self.model is not None
        exact = next(
            (c for c in self.model["classes"] if c["name"] == text.strip()), None
        )
        if exact is not None:
            self.search.clear()  # found: stop dimming the rest
            self.go(exact["id"])

    def search_enter(self) -> None:
        if self.model is None:
            return
        query = self.search.text().strip().lower()
        if not query:
            return
        classes = self.model["classes"]
        hit = next((c for c in classes if c["name"].lower() == query), None) or next(
            (c for c in classes if query in c["name"].lower()), None
        )
        if hit is not None:
            self.search.clear()
            self.go(hit["id"])

    def go_name(self, name: str) -> None:
        if self.model is None:
            return
        hit = next((c for c in self.model["classes"] if c["name"] == name), None)
        if hit is not None:
            QTimer.singleShot(0, self.search.clear)
            self.go(hit["id"])

    # -- a class -------------------------------------------------------------------------

    def go(self, cid: str) -> None:
        if cid not in self.by_id:
            return
        if cid not in self.items:
            # not drawn under the current filters: show it with its neighbours
            self.focus = cid
            self.show_focus()
            self.render(
                fit=True, then=[lambda: self.choose(cid), lambda: self.centre(cid)]
            )
            return
        self.choose(cid)
        self.centre(cid)

    def centre(self, cid: str | None) -> None:
        if cid and (item := self.items.get(cid)) is not None:
            self.canvas.centre_on(item.sceneBoundingRect())

    def show_focus(self) -> None:
        if self.focus and self.focus in self.by_id:
            name = html.escape(self.by_id[self.focus]["name"])
            self.focus_label.setText(f"Only <b><tt>{name}</tt></b> and its neighbours")
            self.focus_bar.show()
        else:
            self.focus = None
            self.focus_bar.hide()

    def unfocus(self) -> None:
        self.focus = None
        self.show_focus()
        chosen = self.chosen
        self.render(fit=True, then=[lambda: self.choose(chosen)] if chosen else [])

    def toggle_focus(self) -> None:
        self.focus = None if self.focus == self.chosen else self.chosen
        self.show_focus()
        chosen = self.chosen
        self.render(fit=True, then=[lambda: self.choose(chosen)])

    def choose(self, cid: str | None) -> None:
        if cid is not None and cid not in self.by_id:
            cid = None
        self.chosen = cid
        self.mark()
        if cid is None:
            self.detail.hide()
            return
        assert self.model is not None
        c = self.by_id[cid]
        self.detail_name.setText(c["name"])
        self.detail_kind.setText(c["kind"])
        self.detail_where.setText(f"{c['module']}<br>{html.escape(c['where'])}")
        self.focus_button.setText(
            "Show everything" if self.focus == cid else "Only its neighbours"
        )

        def prose(s: str) -> str:
            return re.sub(r"``(.+?)``", r"<code>\1</code>", html.escape(s))

        parts = [
            f"<p>{prose(c['doc'])}</p>" if c["doc"] else "<p><i>No docstring.</i></p>"
        ]
        fields = "".join(
            f"<li class='{'private' if f['private'] else ''}'><span class='sig'>"
            f"{html.escape(f['name'])}{': ' + html.escape(f['type']) if f['type'] else ''}</span></li>"
            for f in c["fields"]
        )
        if fields:
            parts.append(
                f"<h3>{'Members' if c['kind'] == 'enumeration' else 'Fields'}</h3><ul>{fields}</ul>"
            )
        methods = "".join(
            f"<li class='{'private' if m['private'] else ''}'><span class='sig'>"
            f"{'async ' if m['async'] else ''}{html.escape(m['signature'])}</span>"
            f"{'<br><small>' + prose(m['doc']) + '</small>' if m['doc'] else ''}</li>"
            for m in c["methods"]
        )
        if methods:
            parts.append(f"<h3>Methods</h3><ul>{methods}</ul>")
        for kind, end, title in HEADINGS:
            other = "to" if end == "from" else "from"
            found = [
                (self.by_id[r[other]], r.get("label", ""))
                for r in self.model["relations"]
                if r["kind"] == kind and r[end] == cid
            ]
            if found:
                links = "".join(
                    f"<li><a href='go:{t['id']}'>{html.escape(t['name'])}</a>"
                    f"{' <small>' + html.escape(label) + '</small>' if label else ''}</li>"
                    for t, label in found
                )
                parts.append(f"<h3>{title}</h3><ul>{links}</ul>")
        self.detail_text.setHtml("".join(parts))
        self.detail.show()

    def show_source(self, cid: str | None) -> None:
        if not cid:
            return
        c = self.by_id[cid]
        text = self.sources.get(cid, "The source could not be read.")
        SourceDialog(self, c["where"], text, self.colours, python=True).exec()

    def open_in_editor(self, cid: str | None) -> None:
        if not cid or not self.by_id[cid]["where"]:
            return
        where = self.by_id[cid]["where"]
        path, _, line = where.rpartition(":")
        target = self.repo / path
        if (code := shutil.which("code")) is not None:
            QProcess.startDetached(code, ["-g", f"{target}:{line}"])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # -- the Mermaid text ----------------------------------------------------------------

    def copy_text(self) -> None:
        QGuiApplication.clipboard().setText(self.text)
        self.copy_button.setText("Copied")
        QTimer.singleShot(1500, lambda: self.copy_button.setText("Copy"))

    def save_text(self) -> None:
        package = self.model["package"] if self.model else "intercede"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the Mermaid text", f"{package}-classes.mmd", "Mermaid (*.mmd)"
        )
        if path:
            Path(path).write_text(self.text)

    def view_text(self) -> None:
        SourceDialog(
            self,
            "The Mermaid text of what is drawn",
            self.text,
            self.colours,
            python=False,
        ).exec()

    # -- closing --------------------------------------------------------------------------

    def stop(self) -> None:
        """Stop reading and laying out: nothing may call back into a closed window."""
        self.debounce.stop()
        self.reader.stop()
        self.layouter.stop()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt's name)
        self.stored.setValue("geometry", self.saveGeometry())
        self.stop()
        super().closeEvent(event)
