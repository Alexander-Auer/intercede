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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QFileSystemWatcher,
    QProcess,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QCompleter,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsScene,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import diagram as d
from .items import AreaItem, ClassItem, EdgeItem, Fonts, text_size
from .journey import TracePage
from .style import (
    THEMES,
    Switch,
    caps,
    icon,
    kind_colour,
    legend_arrow,
    sans,
    stylesheet,
    svg,
    swatch,
)
from .widgets import AreaRow, Canvas, CodeView, PythonHighlighter, tool
from .work import (
    Layouter,
    Reader,
    Tracer,
    branch,
    cache_dir,
    checkout_python,
    icon_svg,
)

__all__ = ["Window", "checkout_python", "icon_svg"]

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
ARROWS = [
    ("inherits", "inherits from"),
    ("implements", "implements the protocol"),
    ("holds", "holds, as a field"),
    ("uses", "uses, in a method"),
]
KINDS = [
    ("protocol", "protocol"),
    ("model", "model or dataclass"),
    ("enumeration", "enumeration"),
    ("exception", "exception"),
    ("class", "class"),
]


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    label.setFont(caps(10.5))
    return label


def _link(text: str, tip: str, act: Callable[[], Any]) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tip)
    button.setObjectName("link")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(lambda _checked=False: act())
    return button


def _with_icon(button: QAbstractButton, name: str) -> QAbstractButton:
    button.setProperty("icon_name", name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class SourceDialog(QDialog):
    """A class's source with its line numbers, or the Mermaid text of what is drawn."""

    def __init__(
        self,
        parent: Window,
        title: str,
        subtitle: str,
        text: str,
        *,
        first: int = 1,
        python: bool = False,
        open_it: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 760)
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)
        top = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("title")
        name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        heading.addWidget(name)
        where = QLabel(subtitle)
        where.setObjectName("where")
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        heading.addWidget(where)
        top.addLayout(heading, 1)
        c = parent.colours
        copy = QPushButton(icon("copy", c["ink-2"]), " Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(text))
        copy.clicked.connect(lambda: copy.setText(" Copied"))
        top.addWidget(copy, 0, Qt.AlignmentFlag.AlignTop)
        if open_it is not None:
            editor = QPushButton(icon("open", c["ink-2"]), " Open in editor")
            editor.clicked.connect(open_it)
            top.addWidget(editor, 0, Qt.AlignmentFlag.AlignTop)
        close = QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        top.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(top)
        view = CodeView(text, c, first)
        if python:
            self.highlighter = PythonHighlighter(view.document(), c)
        column.addWidget(view, 1)


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
        if self.theme_choice not in ("system", *THEMES):  # an older or broken setting
            self.theme_choice = "system"
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
        self.edges: list[EdgeItem] = []
        self.request = 0
        self.then: list[Callable[[], None]] = []  # to do once the next layout is drawn
        self.fit_next = False
        self.open_class = open_class
        key = hashlib.sha256(str(repo).encode()).hexdigest()[:12]
        self.model_file = cache_dir() / f"model-{key}.json"

        self.setWindowTitle(f"interCEde classes — {repo.name}")
        self.setFont(sans(13.5))
        self.scene = QGraphicsScene(self)
        self.scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self.canvas = Canvas(self.scene)
        self.canvas.clicked.connect(self.choose)
        self.canvas.unfocus_button.clicked.connect(self.unfocus)

        # A traced call's path on the diagram: class -> its place, the calls between them.
        self.path: dict[str, int] | None = None
        self.path_calls: list[dict[str, Any]] = []
        self.path_title = ""
        self.mode_buttons: list[QToolButton] = []

        self.tracer = Tracer(repo, python)
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
            self.resize(1560, 960)

        # The last model read opens the window; the code is read again behind it.
        try:
            cached = json.loads(self.model_file.read_text())
        except (OSError, json.JSONDecodeError):
            cached = None
        if cached is not None:
            self.use(cached, read_at=cached.get("read_at", "earlier"), fit=True)
            if self.items:  # drawn from a kept layout already
                self.canvas.show_status(f"{self.counts()} · checking the code…")
        else:
            self.canvas.show_status("Reading the code…")
        self.reader.read()

    # -- building the window ------------------------------------------------------

    def build(self) -> None:
        self.detail = self._detail()
        self.detail.hide()
        self.splitter = QSplitter()
        self.splitter.setHandleWidth(1)
        self.splitter.addWidget(self._side())
        self.splitter.addWidget(self.canvas)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([310, 900, 420])
        self.trace_page = TracePage(self.repo, self.tracer, self._mode_switch())
        self.trace_page.show_in_diagram.connect(self.show_path)
        self.trace_page.open_file.connect(self.open_file)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.splitter)
        self.pages.addWidget(self.trace_page)
        self.setCentralWidget(self.pages)

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

    def _side(self) -> QScrollArea:
        side = QWidget()
        side.setObjectName("side")
        column = QVBoxLayout(side)
        column.setContentsMargins(18, 18, 18, 14)
        column.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.logo = QLabel()
        self.logo.setPixmap(QIcon(str(icon_svg())).pixmap(QSize(30, 30)))
        top.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(1)
        title = QLabel("interCEde classes")
        title.setObjectName("apptitle")
        names.addWidget(title)
        self.caption = QLabel()
        self.caption.setObjectName("subtitle")
        self.caption.setWordWrap(True)
        names.addWidget(self.caption)
        top.addLayout(names, 1)
        top.addWidget(
            tool(
                "refresh",
                "Read the code again (F5)\nIt is also read when a file changes",
                self.reader.read,
            ),
            0,
            Qt.AlignmentFlag.AlignTop,
        )
        column.addLayout(top)
        column.addSpacing(10)
        column.addWidget(self._mode_switch())
        column.addSpacing(10)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a class…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search_icon = QAction(self.search)
        self.search.addAction(
            self.search_icon, QLineEdit.ActionPosition.LeadingPosition
        )
        self.completer = QCompleter([])
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.activated.connect(self.go_name)
        self.search.setCompleter(self.completer)
        self.search.textEdited.connect(self.searched)
        self.search.returnPressed.connect(self.search_enter)
        column.addWidget(self.search)

        column.addWidget(_section("Show"))
        self.switches: dict[str, Switch] = {}
        for key, text in [
            ("members", "Fields and methods"),
            ("private", "Private members"),
            ("uses", "“Uses” arrows"),
            ("exceptions", "Exceptions"),
        ]:
            switch = Switch(text)
            switch.setChecked(getattr(self.settings, key))
            switch.toggled.connect(lambda on, key=key: self.switch(key, on))
            self.switches[key] = switch
            column.addWidget(switch)

        row = QHBoxLayout()
        row.addWidget(_section("Areas"))
        row.addStretch()
        row.addWidget(
            _link("All", "Show every area", lambda: self.set_all(True)),
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        row.addWidget(
            _link("None", "Hide every area", lambda: self.set_all(False)),
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        column.addLayout(row)
        self.areas_box = QVBoxLayout()
        self.areas_box.setSpacing(0)
        column.addLayout(self.areas_box)
        self.area_rows: dict[str, AreaRow] = {}

        column.addWidget(_section("Legend"))
        self.legend: list[
            tuple[QLabel, str, str]
        ] = []  # label, "arrow" or "kind", kind
        for kinds, what in ((ARROWS, "arrow"), (KINDS, "kind")):
            for kind, text in kinds:
                line = QHBoxLayout()
                line.setSpacing(10)
                picture = QLabel()
                picture.setFixedWidth(44)
                picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.legend.append((picture, what, kind))
                line.addWidget(picture)
                words = QLabel(text)
                words.setObjectName("muted")
                line.addWidget(words, 1)
                column.addLayout(line)
            column.addSpacing(6)

        column.addStretch()
        rule = QFrame()
        rule.setObjectName("rule")
        column.addWidget(rule)
        column.addSpacing(6)

        bottom = QHBoxLayout()
        bottom.setSpacing(2)
        mermaid = QLabel("Mermaid")
        mermaid.setObjectName("muted")
        bottom.addWidget(mermaid)
        bottom.addWidget(
            tool("copy", "Copy the Mermaid text of what is drawn", self.copy_text)
        )
        bottom.addWidget(tool("save", "Save it as a .mmd file", self.save_text))
        bottom.addWidget(tool("eye", "View it", self.view_text))
        bottom.addStretch()
        self.themes = QButtonGroup(self)
        for choice, tip in (
            ("system", "As the system"),
            ("light", "Light"),
            ("dark", "Dark"),
        ):
            name = {"system": "system", "light": "sun", "dark": "moon"}[choice]
            button = tool(
                name,
                f"Theme: {tip.lower()}",
                lambda choice=choice: self.set_theme(choice),
            )
            button.setProperty("theme", choice)
            button.setCheckable(True)
            button.setChecked(choice == self.theme_choice)
            self.themes.addButton(button)
            bottom.addWidget(button)
        column.addLayout(bottom)

        self.side = side
        scroll = QScrollArea()
        scroll.setObjectName("sidescroll")
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        self.side_scroll = scroll
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _detail(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("detail")
        panel.setMinimumWidth(360)
        column = QVBoxLayout(panel)
        column.setContentsMargins(22, 18, 18, 10)
        column.setSpacing(6)

        top = QHBoxLayout()
        self.detail_kind = QLabel()
        self.detail_kind.setFont(caps(10.5))
        top.addWidget(self.detail_kind)
        top.addStretch()
        top.addWidget(tool("close", "Close (Esc)", lambda: self.choose(None)))
        column.addLayout(top)

        self.detail_name = QLabel()
        self.detail_name.setObjectName("title")
        self.detail_name.setWordWrap(True)
        self.detail_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        column.addWidget(self.detail_name)
        self.detail_where = QLabel()
        self.detail_where.setObjectName("where")
        self.detail_where.setWordWrap(True)
        self.detail_where.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.detail_where.linkActivated.connect(
            lambda _: self.open_in_editor(self.chosen)
        )
        column.addWidget(self.detail_where)
        column.addSpacing(8)

        actions = QGridLayout()
        actions.setSpacing(6)
        self.focus_button = _with_icon(QPushButton("Neighbours"), "neighbours")
        self.focus_button.setToolTip("Draw only this class and what it touches")
        self.focus_button.clicked.connect(self.toggle_focus)
        actions.addWidget(self.focus_button, 0, 0)
        for place, (name, text, tip, act) in enumerate(
            [
                ("code", "Source", "Its source", lambda: self.show_source(self.chosen)),
                (
                    "centre",
                    "Centre",
                    "Centre the drawing on it",
                    lambda: self.centre(self.chosen),
                ),
                (
                    "open",
                    "Open",
                    "Open it in the editor",
                    lambda: self.open_in_editor(self.chosen),
                ),
            ],
            start=1,
        ):
            button = _with_icon(QPushButton(text), name)
            button.setToolTip(tip)
            button.clicked.connect(act)
            actions.addWidget(button, place // 2, place % 2)
        column.addLayout(actions)
        column.addSpacing(6)
        rule = QFrame()
        rule.setObjectName("rule")
        column.addWidget(rule)

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
        for button in self.themes.buttons():
            button.setChecked(button.property("theme") == choice)
        self.apply_theme()

    def apply_theme(self) -> None:
        choice = self.theme_choice
        if choice == "system":
            scheme = QGuiApplication.styleHints().colorScheme()
            choice = "dark" if scheme == Qt.ColorScheme.Dark else "light"
        c = self.colours = THEMES[choice]
        palette = QPalette()
        for role, colour in {
            QPalette.ColorRole.Window: c["bg"],
            QPalette.ColorRole.WindowText: c["ink"],
            QPalette.ColorRole.Base: c["panel"],
            QPalette.ColorRole.AlternateBase: c["panel-2"],
            QPalette.ColorRole.Text: c["ink"],
            QPalette.ColorRole.Button: c["panel"],
            QPalette.ColorRole.ButtonText: c["ink"],
            QPalette.ColorRole.Highlight: c["accent"],
            QPalette.ColorRole.HighlightedText: "#ffffff",
            QPalette.ColorRole.ToolTipBase: c["panel"],
            QPalette.ColorRole.ToolTipText: c["ink"],
            QPalette.ColorRole.PlaceholderText: c["ink-3"],
            QPalette.ColorRole.Link: c["accent"],
            QPalette.ColorRole.Light: c["panel"],
            QPalette.ColorRole.Midlight: c["panel-2"],
            QPalette.ColorRole.Mid: c["line"],
            QPalette.ColorRole.Dark: c["ink-3"],
            QPalette.ColorRole.Shadow: c["line"],
        }.items():
            palette.setColor(role, QColor(colour))
        QApplication.setPalette(palette)
        check = cache_dir() / f"check-{choice}.svg"
        check.write_text(svg("check", "#ffffff", width=3))
        self.setStyleSheet(stylesheet(c, check))

        for button in self.findChildren(QAbstractButton):
            name = button.property("icon_name")
            if name:
                button.setIcon(icon(name, c["ink-2"]))
        for button in self.canvas.tools:
            button.setIcon(icon(button.property("icon_name"), c["ink-2"]))
        self.search_icon.setIcon(icon("search", c["ink-3"]))
        for switch in self.switches.values():
            switch.colours = c
            switch.update()
        for label, what, kind in self.legend:
            label.setPixmap(
                legend_arrow(kind, c) if what == "arrow" else swatch(c, kind)
            )
        self.canvas.set_colours(c)
        self.trace_page.set_colours(c)
        self.trace_page.run_button.setIcon(icon("play", "#ffffff"))
        self.canvas.setBackgroundBrush(QColor(c["bg"]))
        self.detail_text.document().setDefaultStyleSheet(
            f"""
            p {{ margin: 0; }}
            .doc {{ color: {c["ink"]}; margin-top: 6px; margin-bottom: 4px; }}
            .none {{ color: {c["ink-3"]}; font-style: italic; }}
            .h {{ color: {c["ink-3"]}; font-size: 11px; font-weight: bold; margin-top: 18px;
                  margin-bottom: 6px; }}
            .n {{ color: {c["ink-3"]}; font-weight: normal; }}
            .mono {{ font-family: "Source Code Pro", monospace; font-size: 12px; }}
            .m {{ color: {c["ink"]}; }}
            .t {{ color: {c["ink-2"]}; }}
            .private {{ color: {c["ink-3"]}; }}
            .sig {{ font-family: "Source Code Pro", monospace; font-size: 12px; color: {c["ink"]};
                    margin-top: 8px; }}
            .mdoc {{ color: {c["ink-2"]}; font-size: 12px; margin-top: 2px; }}
            .rel {{ margin-top: 3px; }}
            .label {{ color: {c["ink-3"]}; font-size: 12px; }}
            a {{ color: {c["accent"]}; text-decoration: none; font-weight: bold; }}
            """
        )
        for item in self.scene.items():
            if isinstance(item, ClassItem | EdgeItem | AreaItem):
                item.set_colours(c)
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
        self.trace_page.again()

    def loaded(self, data: dict[str, Any]) -> None:
        data["read_at"] = time.strftime("%H:%M")
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
        shown = "showing the last diagram read" if self.model else "nothing to show yet"
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
        self.settings.hidden &= {a["name"] for a in self.model["areas"]}
        self.build_areas()
        names = sorted({c["name"] for c in self.model["classes"]})
        self.completer.model().setStringList(names)  # type: ignore[attr-defined]
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
        on = f"{html.escape(where)} · " if where else ""
        self.caption.setText(
            f"{on}{len(self.model['classes'])} classes · read {self.read_at}"
        )
        self.caption.setToolTip(
            f"Every class in {self.model['package']}, read from the code in {self.repo}"
            f"{f' on {where}' if where else ''} at {self.read_at}"
        )

    def build_areas(self) -> None:
        assert self.model is not None
        for row in self.area_rows.values():
            row.deleteLater()
        self.area_rows = {}
        counts: dict[str, int] = {}
        for c in self.model["classes"]:
            counts[c["area"]] = counts.get(c["area"], 0) + 1
        for area in self.model["areas"]:
            name = area["name"]
            row = AreaRow(
                d.short(self.model, name),
                area["doc"],
                counts.get(name, 0),
                name not in self.settings.hidden,
                lambda on, name=name: self.toggle_area(name, on),
                lambda name=name: self.only_area(name),
            )
            self.areas_box.addWidget(row)
            self.area_rows[name] = row
        # As wide as its widest row, and room for a scroll bar: nothing is cut off.
        self.side.adjustSize()
        self.side_scroll.setMinimumWidth(self.side.minimumSizeHint().width() + 14)

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
        for name, row in self.area_rows.items():
            row.box.blockSignals(True)
            row.box.setChecked(name not in self.settings.hidden)
            row.box.blockSignals(False)

    def only_area(self, area: str) -> None:
        assert self.model is not None
        self.settings.hidden = {a["name"] for a in self.model["areas"]} - {area}
        self.set_boxes()
        self.focus = None
        self.show_focus()
        self.save_settings()
        self.render(fit=True)

    def set_all(self, on: bool) -> None:
        if self.model is None:
            return
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
        only = set(self.path) if self.path is not None else None
        classes = d.shown_classes(model, self.settings, self.focus, only)
        if self.path is not None:  # the traced call's own arrows: who called whom
            relations = list(self.path_calls)
        else:
            relations = d.shown_relations(
                model, {c["id"] for c in classes}, self.settings
            )
        self.text = d.mermaid_text(model, classes, relations, self.settings)
        self.shown = (classes, relations)
        self.then = list(then or [])
        self.fit_next = fit
        self.request += 1
        if not classes:
            self.scene.clear()
            self.items, self.edges = {}, []
            self.canvas.show_status("Nothing to draw: switch an area on")
            return
        sizes = {
            c["id"]: ClassItem.measure(c, d.members(c, self.settings), self.fonts)
            for c in classes
        }
        texts = {}
        for c in classes:
            title = d.short(model, c["area"])
            w, h = text_size(title, self.fonts.title)
            texts[title] = (w + 24, h + 12)
        for r in relations:
            if r.get("label"):
                w, h = text_size(r["label"], self.fonts.label, pad=3)
                texts[r["label"]] = (w + 10, h + 2)
        dot = d.dot_text(model, classes, relations, sizes, texts)
        if not self.layouter.request(self.request, dot, relations):
            self.canvas.show_status("Laying out…")

    def laid_out(self, number: int, layout: d.Layout) -> None:
        if number != self.request or self.model is None:
            return  # a newer view was asked for meanwhile
        classes, _ = self.shown
        self.scene.clear()
        self.items, self.edges = {}, []
        for area, box in layout.areas.items():
            rect = QRectF(box.x, box.y, box.w, box.h)
            area_item = AreaItem(d.short(self.model, area), rect, self.fonts)
            area_item.set_colours(self.colours)
            self.scene.addItem(area_item)
        for edge in layout.edges:
            item = EdgeItem(edge.relation, edge.points, edge.label_at, self.fonts)
            item.set_colours(self.colours)
            self.scene.addItem(item)
            self.edges.append(item)
        for c in classes:
            box = layout.nodes[c["id"]]
            node = ClassItem(c, d.members(c, self.settings), self.fonts)
            node.set_colours(self.colours)
            node.setPos(box.x, box.y)
            self.scene.addItem(node)
            node.set_order(self.path.get(c["id"]) if self.path is not None else None)
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

    def counts(self) -> str:
        classes, relations = self.shown
        return f"{len(classes)} classes · {len(relations)} arrows"

    def show_counts(self) -> None:
        if self.shown[0]:
            self.canvas.show_status(self.counts())

    def mark(self) -> None:
        """Mark the chosen class and what the search finds; light the chosen class's arrows."""
        query = self.search.text().strip().lower()
        for cid, item in self.items.items():
            found = bool(query) and query in item.name.lower()
            item.set_state(
                chosen=cid == self.chosen, found=found, dimmed=bool(query) and not found
            )
        chosen = self.chosen if self.chosen in self.items else None
        for edge in self.edges:
            if chosen is not None:
                lit = chosen in edge.ends
            elif self.path is not None:  # the calls of the traced call
                lit = edge.kind == "calls"
            else:
                lit = False
            faded = bool(query) or (
                (chosen is not None or self.path is not None) and not lit
            )
            edge.set_state(lit=lit, faded=faded)

    # -- search -----------------------------------------------------------------------

    def searched(self, text: str) -> None:
        self.mark()
        if self.model is None:
            return
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
        if self.path is not None:
            title = html.escape(self.path_title)
            self.canvas.show_focus(f"The path of <b>{title}</b>, in the order it went")
        elif self.focus and self.focus in self.by_id:
            name = html.escape(self.by_id[self.focus]["name"])
            self.canvas.show_focus(f"Only <b>{name}</b> and its neighbours")
        else:
            self.focus = None
            self.canvas.show_focus(None)

    def unfocus(self) -> None:
        self.focus = None
        self.path = None
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
        colour = kind_colour(self.colours, c["kind"])
        self.detail_kind.setText(
            f"<span style='color:{colour}'>● &nbsp;{c['kind']}</span>"
        )
        self.detail_name.setText(c["name"])
        where = html.escape(c["where"])
        self.detail_where.setText(
            f"<span style='font-family:monospace'>{html.escape(c['module'])}</span><br>"
            f"<a href='open' style='color:{self.colours['accent']}; text-decoration:none'>{where}</a>"
        )
        neighbours = self.focus == cid
        self.focus_button.setText("Everything" if neighbours else "Neighbours")
        self.focus_button.setToolTip(
            "Draw every class again"
            if neighbours
            else "Draw only this class and what it touches"
        )
        self.detail_text.setHtml(self._detail_html(c))
        self.detail.show()

    def _detail_html(self, c: dict[str, Any]) -> str:
        assert self.model is not None

        def prose(s: str) -> str:
            return re.sub(r"``(.+?)``", r"<span class='mono'>\1</span>", html.escape(s))

        def heading(title: str, count: int) -> str:
            return (
                f"<p class='h'>{title.upper()} <span class='n'>&nbsp;{count}</span></p>"
            )

        doc = prose(c["doc"]) if c["doc"] else "<span class='none'>No docstring.</span>"
        parts = [f"<p class='doc'>{doc}</p>"]
        if c["fields"]:
            title = "Members" if c["kind"] == "enumeration" else "Fields"
            rows = "".join(
                f"<tr><td class='mono {'private' if f['private'] else 'm'}'>{html.escape(f['name'])}</td>"
                f"<td class='mono t'>{html.escape(f['type'])}</td></tr>"
                for f in c["fields"]
            )
            parts.append(
                heading(title, len(c["fields"]))
                + f"<table width='100%' cellspacing='0' cellpadding='2'>{rows}</table>"
            )
        if c["methods"]:
            methods = []
            for m in c["methods"]:
                signature = html.escape(m["signature"])
                name = html.escape(m["name"])
                signature = signature.replace(name, f"<b>{name}</b>", 1)
                cls = "sig private" if m["private"] else "sig"
                prefix = "<span class='t'>async </span>" if m["async"] else ""
                methods.append(f"<p class='{cls}'>{prefix}{signature}</p>")
                if m["doc"]:
                    methods.append(f"<p class='mdoc'>{prose(m['doc'])}</p>")
            parts.append(heading("Methods", len(c["methods"])) + "".join(methods))
        for kind, end, title in HEADINGS:
            other = "to" if end == "from" else "from"
            found = [
                (self.by_id[r[other]], r.get("label", ""))
                for r in self.model["relations"]
                if r["kind"] == kind and r[end] == c["id"]
            ]
            if not found:
                continue
            links = "".join(
                f"<p class='rel'><span style='color:{kind_colour(self.colours, t['kind'])}'>●</span>"
                f"&nbsp; <a href='go:{t['id']}'>{html.escape(t['name'])}</a>"
                f"{f'&nbsp; <span class=label>{html.escape(label)}</span>' if label else ''}</p>"
                for t, label in found
            )
            parts.append(heading(title, len(found)) + links)
        return "".join(parts)

    def show_source(self, cid: str | None) -> None:
        if not cid:
            return
        c = self.by_id[cid]
        text = self.sources.get(cid, "The source could not be read.")
        line = c["where"].rpartition(":")[2]
        SourceDialog(
            self,
            c["name"],
            c["where"],
            text,
            first=int(line) if line.isdigit() else 1,
            python=True,
            open_it=lambda: self.open_in_editor(cid),
        ).exec()

    def open_in_editor(self, cid: str | None) -> None:
        if not cid or not self.by_id[cid]["where"]:
            return
        path, _, line = self.by_id[cid]["where"].rpartition(":")
        target = self.repo / path
        if (code := shutil.which("code")) is not None:
            QProcess.startDetached(code, ["-g", f"{target}:{line}"])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # -- the Mermaid text ----------------------------------------------------------------

    def copy_text(self) -> None:
        QGuiApplication.clipboard().setText(self.text)
        self.canvas.show_status("Copied the Mermaid text of what is drawn")
        QTimer.singleShot(1800, self.show_counts)

    def save_text(self) -> None:
        package = self.model["package"] if self.model else "intercede"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the Mermaid text", f"{package}-classes.mmd", "Mermaid (*.mmd)"
        )
        if path:
            Path(path).write_text(self.text)

    def view_text(self) -> None:
        classes, relations = self.shown
        SourceDialog(
            self,
            "Mermaid",
            f"The text of what is drawn: {len(classes)} classes, {len(relations)} arrows",
            self.text,
        ).exec()

    # -- the two pages ------------------------------------------------------------------

    def _mode_switch(self) -> QFrame:
        """Diagram or Trace: one switch on each page, kept alike."""
        frame = QFrame()
        frame.setObjectName("segmented")
        row = QHBoxLayout(frame)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        for page, (name, text, tip) in enumerate(
            [
                ("diagram", "Diagram", "Every class, and how they relate"),
                ("trace", "Trace a call", "Follow one call through the code, and back"),
            ]
        ):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tip)
            button.setProperty("icon_name", name)
            button.setProperty("page", page)
            button.setCheckable(True)
            button.setChecked(page == 0)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, page=page: self.show_page(page))
            row.addWidget(button)
            self.mode_buttons.append(button)
        return frame

    def show_page(self, page: int) -> None:
        self.pages.setCurrentIndex(page)
        for button in self.mode_buttons:
            button.setChecked(button.property("page") == page)
        if page == 1:
            self.trace_page.start(self.model)

    def show_path(
        self, order: dict[str, int], calls: list[dict[str, Any]], title: str
    ) -> None:
        """Draw the classes a traced call went through, numbered, with its calls as arrows."""
        self.path = order or None
        self.path_calls = calls
        self.path_title = title
        self.focus = None
        self.chosen = None
        self.detail.hide()
        self.show_focus()
        self.show_page(0)
        self.render(fit=True)

    def open_file(self, path: str, line: int) -> None:
        target = self.repo / path
        if (code := shutil.which("code")) is not None:
            QProcess.startDetached(code, ["-g", f"{target}:{line}"])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # -- closing --------------------------------------------------------------------------

    def stop(self) -> None:
        """Stop reading and laying out: nothing may call back into a closed window."""
        self.debounce.stop()
        self.reader.stop()
        self.layouter.stop()
        self.tracer.stop()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt's name)
        self.stored.setValue("geometry", self.saveGeometry())
        self.stop()
        super().closeEvent(event)
