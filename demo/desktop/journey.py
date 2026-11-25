"""The Trace page: how one call travels through the code, and back.

A call is recorded by ``trace.py`` (in the checkout's own Python): every function of
``intercede`` it runs, and what leaves the process. This page shows that one run
three ways at once:

- **the calls, as a tree**: who called whom, what each returned, and what went out
  (a command, a request) with its answer;
- **the code** of the call that is selected, at the line the run is at;
- **step by step**, forward and back, as a debugger would have gone through it:
  into each call, and out again with what it returned.

*Show in diagram* draws the classes the call went through, numbered in the order
it reached them, with the arrows it followed lit.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import diagram as d
from .diagram import function_source
from .style import caps, icon, mix, mono
from .widgets import CodeView, tool
from .work import Tracer

OPERATIONS = {
    "submit": "submit",
    "get_status": "get_status",
    "get_output": "get_output",
    "kill": "kill",
    "purge": "purge",
    "get_diagnostics": "get_diagnostics",
}
_NODE = Qt.ItemDataRole.UserRole


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    label.setFont(caps(10.5))
    return label


class TracePage(QWidget):
    """Choose a backend and an operation, run it, and follow the call."""

    show_in_diagram = Signal(
        dict, list, str
    )  # class -> order, the calls between them, title
    open_file = Signal(str, int)  # path in the checkout, line

    def __init__(self, repo: Path, tracer: Tracer, top: QWidget) -> None:
        super().__init__()
        self.repo = repo
        self.tracer = tracer
        self.colours: dict[str, str] = {}
        self.backends: list[dict[str, Any]] = []
        self.backend = ""
        self.operation = "submit"
        self.trace: dict[str, Any] | None = None
        self.nodes: list[dict[str, Any]] = []
        self.children: dict[int | None, list[int]] = {}
        self.items: dict[int, QTreeWidgetItem] = {}
        self.events: list[
            tuple[int, str]
        ] = []  # (node, "in" or "out"), in the order they ran
        self.position = 0
        self.files: dict[str, str] = {}
        self.model: dict[str, Any] | None = None

        tracer.listed.connect(self.listed)
        tracer.traced.connect(self.traced)
        tracer.failed.connect(self.failed)
        tracer.progress.connect(lambda text: self.result.setText(html.escape(text)))

        splitter = QSplitter()
        splitter.setHandleWidth(1)
        splitter.addWidget(self._side(top))
        splitter.addWidget(self._calls())
        splitter.addWidget(self._detail())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([310, 640, 620])
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(splitter)

        for key, act in [
            ("Right", self.step),
            ("Left", self.back),
            ("Shift+Right", self.step_over),
            ("Home", lambda: self.go_to(0)),
        ]:
            shortcut = QShortcut(QKeySequence(key), self, act)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # -- building ----------------------------------------------------------------------

    def _side(self, top: QWidget) -> QScrollArea:
        side = QWidget()
        side.setObjectName("side")
        column = QVBoxLayout(side)
        column.setContentsMargins(18, 18, 18, 14)
        column.setSpacing(6)
        column.addWidget(top)

        column.addWidget(_section("Backend"))
        self.backend_box = QVBoxLayout()
        self.backend_box.setSpacing(6)
        column.addLayout(self.backend_box)
        self.backend_group = QButtonGroup(self)
        self.about = QLabel()
        self.about.setObjectName("muted")
        self.about.setWordWrap(True)
        column.addWidget(self.about)

        column.addWidget(_section("Operation"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.operation_group = QButtonGroup(self)
        self.operation_buttons: dict[str, QPushButton] = {}
        for place, operation in enumerate(OPERATIONS):
            button = QPushButton(operation)
            button.setObjectName("choice")
            button.setCheckable(True)
            button.setFont(mono(12.5))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _=False, op=operation: self.choose_operation(op)
            )
            self.operation_group.addButton(button)
            self.operation_buttons[operation] = button
            grid.addWidget(button, place // 2, place % 2)
        column.addLayout(grid)
        column.addSpacing(10)

        self.run_button = QPushButton("Trace")
        self.run_button.setObjectName("primary")
        self.run_button.setProperty("icon_name", "play")
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.setMinimumHeight(36)
        self.run_button.clicked.connect(self.run)
        column.addWidget(self.run_button)

        column.addWidget(_section("Result"))
        self.result = QLabel("Choose a backend and an operation, then Trace.")
        self.result.setObjectName("muted")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        column.addWidget(self.result)

        column.addWidget(_section("Legend"))
        self.legend: list[tuple[QLabel, str]] = []
        for name, text in [
            ("call", "a function of interCEde"),
            ("async", "an async function: awaited"),
            ("command", "a program it runs"),
            ("request", "an HTTP request it sends"),
            ("copy", "a file it copies over SSH"),
            ("raised", "it raised an exception"),
        ]:
            row = QHBoxLayout()
            row.setSpacing(10)
            picture = QLabel()
            picture.setFixedWidth(22)
            self.legend.append((picture, name))
            row.addWidget(picture)
            words = QLabel(text)
            words.setObjectName("muted")
            row.addWidget(words, 1)
            column.addLayout(row)
        keys = QLabel("→ / ← step through it, Shift+→ steps over a call")
        keys.setObjectName("muted")
        keys.setWordWrap(True)
        column.addSpacing(8)
        column.addWidget(keys)
        column.addStretch()

        scroll = QScrollArea()
        scroll.setObjectName("sidescroll")
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        self.side, self.side_scroll = side, scroll
        self.fit_side()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _calls(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("calls")
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("stepbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(2)
        self.step_tools = [
            tool("first", "To the start (Home)", lambda: self.go_to(0)),
            tool("back", "A step back (←)", self.back),
            tool("step", "A step forward (→)", self.step),
            tool("over", "Step over this call (Shift+→)", self.step_over),
        ]
        for button in self.step_tools:
            row.addWidget(button)
        row.addSpacing(8)
        self.where = QLabel("No trace yet")
        self.where.setObjectName("muted")
        row.addWidget(self.where, 1)
        self.diagram_button = QPushButton("Show in diagram")
        self.diagram_button.setProperty("icon_name", "diagram")
        self.diagram_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.diagram_button.setToolTip(
            "Draw the classes this call went through, in order"
        )
        self.diagram_button.clicked.connect(self.to_diagram)
        row.addWidget(self.diagram_button)
        self.tree_tools = [
            tool("expand", "Open every call", lambda: self.tree.expandAll()),
            tool("collapse", "Show the main path only", self.open_main_path),
        ]
        for button in self.tree_tools:
            row.addWidget(button)
        column.addWidget(bar)
        rule = QFrame()
        rule.setObjectName("rule")
        column.addWidget(rule)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Call", "Returned", "ms"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setIndentation(18)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.resizeSection(0, 330)
        self.tree.itemSelectionChanged.connect(self.selected)
        column.addWidget(self.tree, 1)
        return panel

    def _detail(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("detail")
        column = QVBoxLayout(panel)
        column.setContentsMargins(20, 16, 16, 12)
        column.setSpacing(6)
        self.kind = QLabel()
        self.kind.setFont(caps(10.5))
        column.addWidget(self.kind)
        self.title = QLabel("—")
        self.title.setObjectName("title")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.title)
        self.place = QLabel()
        self.place.setObjectName("where")
        self.place.setWordWrap(True)
        self.place.linkActivated.connect(self.link)
        column.addWidget(self.place)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setHandleWidth(8)
        self.info = QTextBrowser()
        self.info.setOpenLinks(False)
        self.info.anchorClicked.connect(lambda url: self.link(url.toString()))
        split.addWidget(self.info)
        code = QWidget()
        code_column = QVBoxLayout(code)
        code_column.setContentsMargins(0, 0, 0, 0)
        code_column.setSpacing(4)
        self.code_title = _section("Code")
        code_column.addWidget(self.code_title)
        self.code = CodeView("", {"panel": "#ffffff", "ink-3": "#888888"})
        code_column.addWidget(self.code, 1)
        split.addWidget(code)
        split.setSizes([260, 520])
        column.addWidget(split, 1)
        return panel

    # -- theme ---------------------------------------------------------------------------

    def set_colours(self, colours: dict[str, str]) -> None:
        self.colours = colours
        self.code.colours = colours
        self.code.gutter.update()
        from .widgets import PythonHighlighter

        self.highlighter = PythonHighlighter(self.code.document(), colours)
        for label, name in self.legend:
            colour = colours["failed"] if name == "raised" else self._icon_colour(name)
            label.setPixmap(icon(name, colour, 16).pixmap(16, 16))
        self.info.document().setDefaultStyleSheet(
            f"""
            p {{ margin: 0; }}
            .h {{ color: {colours["ink-3"]}; font-size: 11px; font-weight: bold; margin-top: 14px;
                  margin-bottom: 4px; }}
            .k {{ font-family: "Source Code Pro", monospace; color: {colours["ink-2"]}; }}
            .v {{ font-family: "Source Code Pro", monospace; color: {colours["ink"]}; }}
            .err {{ font-family: "Source Code Pro", monospace; color: {colours["failed"]}; }}
            .pre {{ font-family: "Source Code Pro", monospace; color: {colours["ink"]};
                    white-space: pre-wrap; }}
            .muted {{ color: {colours["ink-3"]}; }}
            a {{ color: {colours["accent"]}; text-decoration: none; font-weight: bold; }}
            """
        )
        if self.trace is not None:
            self.fill_tree()
            self.go_to(self.position)

    def _icon_colour(self, kind: str) -> str:
        c = self.colours
        return {
            "call": c["ink-2"],
            "async": c["accent"],
            "command": c["waiting"],
            "request": c["done"],
            "copy": c["done"],
        }.get(kind, c["ink-2"])

    # -- backends and operations ------------------------------------------------------------

    def start(self, model: dict[str, Any] | None) -> None:
        """The page is shown: ask which backends the checkout has, the first time."""
        self.model = model
        if not self.backends:
            self.result.setText("Looking for the backends of this checkout…")
            self.tracer.list()

    def listed(self, backends: list[dict[str, Any]]) -> None:
        self.backends = backends
        while self.backend_box.count():
            widget = self.backend_box.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for button in self.backend_group.buttons():
            self.backend_group.removeButton(button)
        for backend in backends:
            running = backend.get("running")
            mark = {True: "●", False: "○", None: "◌"}[running]
            button = QPushButton(f"{mark}  {backend['title']}")
            button.setObjectName("choice")
            button.setCheckable(True)
            button.setToolTip(backend["about"])
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _=False, name=backend["name"]: self.choose_backend(name)
            )
            self.backend_group.addButton(button)
            self.backend_box.addWidget(button)
            button.setProperty("backend", backend["name"])
        self.fit_side()
        names = [b["name"] for b in backends]
        self.choose_backend(self.backend if self.backend in names else names[0])
        self.result.setText("Choose a backend and an operation, then Trace.")

    def fit_side(self) -> None:
        """As wide as what the side bar holds, and room for a scroll bar: nothing is cut off."""
        self.side.adjustSize()
        self.side_scroll.setMinimumWidth(self.side.minimumSizeHint().width() + 14)

    def choose_backend(self, name: str) -> None:
        self.backend = name
        backend = next(b for b in self.backends if b["name"] == name)
        for button in self.backend_group.buttons():
            button.setChecked(button.property("backend") == name)
        about = html.escape(backend["about"])
        if backend.get("running") is False:
            about += (
                "<br><br><b>Not running.</b> Start it from the checkout with<br>"
                f"<span style='font-family:monospace'>{html.escape(backend['start'])}</span>"
            )
        elif backend.get("running") is None:
            about += "<br><br>Whether it runs is not known: Docker did not say."
        self.about.setText(about)
        self.about.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for operation, button in self.operation_buttons.items():
            button.setEnabled(operation in backend["operations"])
        if self.operation not in backend["operations"]:
            self.operation = "submit"
        self.choose_operation(self.operation)

    def choose_operation(self, operation: str) -> None:
        self.operation = operation
        for name, button in self.operation_buttons.items():
            button.setChecked(name == operation)
        self.run_button.setText(f"  Trace {operation}()")

    def run(self) -> None:
        if not self.backend:
            return
        self.run_button.setEnabled(False)
        self.result.setText(f"Starting {self.operation}()…")
        self.tracer.run(self.backend, self.operation)

    def again(self) -> None:
        """The code changed: record the same call again."""
        if self.trace is not None and self.isVisible():
            self.tracer.run(self.trace["backend"], self.trace["operation"])

    def failed(self, reason: str) -> None:
        self.run_button.setEnabled(True)
        self.result.setText(
            f"<span style='color:{self.colours.get('failed', 'red')}'>"
            f"{html.escape(reason)}</span>"
        )

    # -- a trace ----------------------------------------------------------------------------

    def traced(self, trace: dict[str, Any]) -> None:
        self.run_button.setEnabled(True)
        self.trace = trace
        self.nodes = trace["nodes"]
        self.files = {}
        self.children = {}
        for node in self.nodes:
            self.children.setdefault(node["parent"], []).append(node["id"])
        steps = []
        for node in self.nodes:
            steps.append((node["step_in"], node["id"], "in"))
            if "step_out" in node:
                steps.append((node["step_out"], node["id"], "out"))
        self.events = [(number, way) for _, number, way in sorted(steps)]
        calls = sum(n["kind"] == "call" for n in self.nodes)
        commands = sum(n["kind"] == "command" for n in self.nodes)
        requests = sum(n["kind"] == "request" for n in self.nodes)
        out = []
        if commands:
            out.append(f"{commands} command{'s' * (commands != 1)}")
        if requests:
            out.append(f"{requests} request{'s' * (requests != 1)}")
        counts = " · ".join([f"{calls} calls", *out, f"{trace['duration']:.0f} ms"])
        returned = trace.get("result")
        job = (
            f"<br>The job was {trace['job'].lower()} when the call was made."
            if "job" in trace
            else ""
        )
        outcome = (
            f"<span style='color:{self.colours['failed']}'>raised {html.escape(trace['error'])}</span>"
            if "error" in trace
            else f"returned <span style='font-family:monospace'>{html.escape(str(returned))}</span>"
        )
        self.result.setText(
            f"<b>{html.escape(trace['class'])}.{html.escape(trace['operation'])}()</b> {outcome}"
            f"<br><br>{counts}{job}"
        )
        self.fill_tree()
        self.go_to(0)

    def fill_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        self.items = {}
        mono_font = mono(12.5)
        bold = QFont(mono_font)
        bold.setBold(True)

        def add(number: int, parent: QTreeWidgetItem | QTreeWidget) -> None:
            node = self.nodes[number]
            kind = self._kind(node)
            item = QTreeWidgetItem(parent)
            item.setText(0, node["name"])
            item.setText(1, node.get("raised") or node.get("returned") or "")
            took = node["end"] - node["start"] if "end" in node else None
            item.setText(2, f"{took:.1f}" if took is not None else "")
            item.setTextAlignment(
                2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            item.setData(0, _NODE, number)
            colour = (
                self.colours["failed"] if kind == "raised" else self._icon_colour(kind)
            )
            item.setIcon(0, icon(kind, colour, 16))
            item.setFont(0, bold if node["kind"] != "call" else mono_font)
            item.setFont(1, mono_font)
            if node["kind"] != "call":
                item.setForeground(0, QBrush(QColor(colour)))
            if kind == "raised":
                item.setForeground(1, QBrush(QColor(self.colours["failed"])))
            else:
                item.setForeground(1, QBrush(QColor(self.colours["ink-3"])))
            item.setToolTip(0, node.get("module", node.get("name", "")))
            self.items[number] = item
            for child in self.children.get(number, []):
                add(child, item)

        for root in self.children.get(None, []):
            add(root, self.tree)
        self.tree.blockSignals(False)
        self.open_main_path()

    def open_main_path(self) -> None:
        """Open the calls that lead out of the process, and the first level; close the rest."""
        self.tree.collapseAll()
        leading: set[int] = set()
        for node in self.nodes:
            if node["kind"] != "call" or "raised" in node:
                parent = node["parent"]
                while parent is not None and parent not in leading:
                    leading.add(parent)
                    parent = self.nodes[parent]["parent"]
        for root in self.children.get(None, []):
            leading.add(root)
        for number in leading:
            self.items[number].setExpanded(True)

    @staticmethod
    def _kind(node: dict[str, Any]) -> str:
        if "raised" in node or node.get("failed"):
            return "raised"
        if node["kind"] != "call":
            return node["kind"]
        return "async" if node.get("async") else "call"

    # -- stepping -----------------------------------------------------------------------------

    def step(self) -> None:
        self.go_to(self.position + 1)

    def back(self) -> None:
        self.go_to(self.position - 1)

    def step_over(self) -> None:
        if not self.events:
            return
        number, way = self.events[self.position]
        if way == "in":
            out = next(
                (i for i, e in enumerate(self.events) if e == (number, "out")), None
            )
            self.go_to(out if out is not None else self.position + 1)
        else:
            self.go_to(self.position + 1)

    def go_to(self, position: int) -> None:
        if not self.events:
            return
        self.position = max(0, min(position, len(self.events) - 1))
        number, way = self.events[self.position]
        item = self.items.get(number)
        if item is not None:
            self.tree.blockSignals(True)
            parent = item.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
            self.tree.blockSignals(False)
        self.show_node(number, way)

    def selected(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        number = item.data(0, _NODE)
        self.position = self.events.index((number, "in"))
        self.show_node(number, "in")

    # -- one call --------------------------------------------------------------------------

    def show_node(self, number: int, way: str) -> None:
        node = self.nodes[number]
        c = self.colours
        kind = self._kind(node)
        colour = c["failed"] if kind == "raised" else self._icon_colour(kind)
        label = {
            "call": "call",
            "async": "async call",
            "command": "command",
            "request": "request",
            "copy": "file copy",
            "raised": "raised",
        }[kind]
        self.kind.setText(f"<span style='color:{colour}'>● &nbsp;{label}</span>")
        self.title.setText(html.escape(node["name"]))
        caller = self.nodes[node["parent"]] if node["parent"] is not None else None

        total = len(self.events)
        if node["kind"] == "call":
            verb = (
                "enters"
                if way == "in"
                else ("raises from" if "raised" in node else "returns from")
            )
        else:
            verb = "sends" if way == "in" else "gets the answer to"
        self.where.setText(
            f"<b>{self.position + 1}</b> / {total} &nbsp;·&nbsp; {verb} "
            f"<b>{html.escape(_clip(node['name'], 44))}</b>"
        )

        if node["kind"] == "call":
            place = (
                f"<span style='font-family:monospace'>{html.escape(node['module'])}</span><br>"
                f"<a href='open:{node['file']}:{node['first_line']}' "
                f"style='color:{c['accent']}; text-decoration:none'>"
                f"{html.escape(node['file'])}:{node['first_line']}</a>"
            )
        else:
            place = "Leaves the process: what interCEde sends, and the answer it gets."
        self.place.setText(place)

        parts = []
        if caller is not None:
            line = node.get("call_line")
            at = f", line {line}" if line else ""
            parts.append(
                f"<p class='h'>CALLED BY</p><p><a href='node:{caller['id']}'>"
                f"{html.escape(caller['name'])}</a><span class='muted'>{at}</span></p>"
            )
        if node["kind"] == "call":
            if node["args"]:
                rows = "".join(
                    f"<tr><td class='k'>{html.escape(name)}&nbsp;&nbsp;</td>"
                    f"<td class='v'>{html.escape(value)}</td></tr>"
                    for name, value in node["args"].items()
                )
                parts.append(
                    f"<p class='h'>ARGUMENTS</p><table cellspacing='0' cellpadding='2'>{rows}</table>"
                )
            if "raised" in node:
                parts.append(
                    f"<p class='h'>RAISED</p><p class='err'>{html.escape(node['raised'])}</p>"
                )
            elif "returned" in node:
                parts.append(
                    f"<p class='h'>RETURNED</p><p class='v'>{html.escape(node['returned'])}</p>"
                )
            calls = [self.nodes[n] for n in self.children.get(number, [])]
            if calls:
                links = "".join(
                    f"<p><span class='muted'>line {k.get('call_line') or '?'}&nbsp;&nbsp;</span>"
                    f"<a href='node:{k['id']}'>{html.escape(k['name'][:70])}</a></p>"
                    for k in calls
                )
                parts.append(f"<p class='h'>CALLS {len(calls)}</p>{links}")
        else:
            if node.get("sent"):
                parts.append(
                    f"<p class='h'>SENT</p><p class='pre'>{html.escape(node['sent'][:1500])}</p>"
                )
            if "returned" in node:
                cls = "err" if node.get("failed") else "v"
                parts.append(
                    f"<p class='h'>ANSWER</p><p class='{cls}'>{html.escape(node['returned'])}</p>"
                )
            if node.get("detail"):
                parts.append(f"<p class='pre'>{html.escape(node['detail'][:3000])}</p>")
        self.info.setHtml("".join(parts))
        self.show_code(node, way, caller)

    def show_code(
        self, node: dict[str, Any], way: str, caller: dict[str, Any] | None
    ) -> None:
        """The code the run is in: the call's own, or, for what leaves the process, its caller's."""
        c = self.colours
        shown = node if node["kind"] == "call" else caller
        if shown is None:
            self.code.set_code("", 1)
            return
        text = self._file(shown["file"])
        source, first = function_source(text, shown["first_line"])
        self.code.set_code(source, first)
        here = mix(c["focus"], 0.28, c["panel"]).name()
        calls = mix(c["accent"], 0.10, c["panel"]).name()
        marks = {
            k.get("call_line"): calls
            for k in (self.nodes[n] for n in self.children.get(shown["id"], []))
            if k.get("call_line")
        }
        if node["kind"] != "call":
            at = node.get("call_line")
        elif way == "in":
            at = shown["first_line"]
            lines = source.splitlines()
            for offset, line in enumerate(lines):  # the def line, past decorators
                if line.lstrip().startswith(("def ", "async def ")):
                    at = first + offset
                    break
        else:
            at = node.get("return_line")
        if at:
            marks[at] = here
        self.code.mark(marks, centre=at)

    def _file(self, path: str) -> str:
        if path not in self.files:
            try:
                self.files[path] = (self.repo / path).read_text()
            except OSError:
                self.files[path] = ""
        return self.files[path]

    def link(self, target: str) -> None:
        if target.startswith("node:"):
            number = int(target.removeprefix("node:"))
            self.go_to(self.events.index((number, "in")))
        elif target.startswith("open:"):
            path, _, line = target.removeprefix("open:").rpartition(":")
            self.open_file.emit(path, int(line))

    # -- the path, on the diagram ----------------------------------------------------------------

    def to_diagram(self) -> None:
        """The classes the call went through, in the order it reached them."""
        if self.trace is None or self.model is None:
            return
        order, calls = d.trace_path(self.model, self.nodes)
        title = f"{self.trace['class']}.{self.trace['operation']}()"
        self.show_in_diagram.emit(order, calls, title)


def _clip(text: str, length: int) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"
