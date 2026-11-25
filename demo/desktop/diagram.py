"""What the diagram shows, as plain data: the filters, the Mermaid text, and the Graphviz layout.

Nothing here draws or imports Qt, so it is tested on its own. The model is the one
``demo/classes.py`` reads from the package (``model()``), as the web page gets it.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Model = Mapping[str, Any]
Class = Mapping[str, Any]
Relation = Mapping[str, Any]

POINTS_PER_INCH = 72  # Graphviz sizes nodes in inches and places them in points


@dataclass
class Settings:
    """What to show; the same switches as the web page, with the same defaults."""

    members: bool = False  # the whole package reads best as names and arrows
    private: bool = False
    uses: bool = False
    exceptions: bool = True
    hidden: set[str] = field(default_factory=set)  # areas switched off


# -- what is drawn ------------------------------------------------------------------


def shown_classes(model: Model, settings: Settings, focus: str | None) -> list[Class]:
    """The classes to draw: a focused class and its neighbours, or what the filters let through."""
    if focus:
        near = {focus}
        for r in model["relations"]:
            if r["kind"] == "uses" and not settings.uses:
                continue
            if r["from"] == focus:
                near.add(r["to"])
            if r["to"] == focus:
                near.add(r["from"])
        return [c for c in model["classes"] if c["id"] in near]
    return [
        c
        for c in model["classes"]
        if c["area"] not in settings.hidden
        and (settings.exceptions or c["kind"] != "exception")
    ]


def shown_relations(model: Model, ids: set[str], settings: Settings) -> list[Relation]:
    """The arrows between drawn classes; "uses" only when switched on."""
    return [
        r
        for r in model["relations"]
        if r["from"] in ids
        and r["to"] in ids
        and (r["kind"] != "uses" or settings.uses)
    ]


def short(model: Model, area: str) -> str:
    """An area without the package's name in front."""
    package = model["package"]
    return area if area == package else area[len(package) + 1 :]


@dataclass(frozen=True)
class Member:
    """One line of a class box: a field or a method, as Python writes it."""

    text: str
    static: bool = False  # drawn underlined, as UML does


def members(cls: Class, settings: Settings) -> list[Member]:
    """The member lines of a class box; none unless members are shown."""
    if not settings.members:
        return []
    lines = []
    for f in cls["fields"]:
        if f["private"] and not settings.private:
            continue
        typed = f": {f['type']}" if f["type"] else ""
        lines.append(Member(f"{'-' if f['private'] else '+'}{f['name']}{typed}"))
    for m in cls["methods"]:
        if m["private"] and not settings.private:
            continue
        returns = f" {m['returns']}" if m["returns"] and m["returns"] != "None" else ""
        prefix = f"{'-' if m['private'] else '+'}{'async ' if m['async'] else ''}"
        text = f"{prefix}{m['name']}({', '.join(m['parameters'])}){returns}"
        lines.append(Member(text, static=m["static"]))
    return lines


# -- Mermaid text, as the web page writes it --------------------------------------------

# Mermaid would read ~ as a generic, __ and * as markdown, and { } " < > as syntax, so
# those go in as its entity codes.
_ENTITIES = {
    "~": "#126;",
    "_": "#95;",
    "*": "#42;",
    "{": "#123;",
    "}": "#125;",
    '"': "#34;",
    "<": "#60;",
    ">": "#62;",
}
_ARROWS = {"inherits": "<|--", "implements": "<|..", "holds": "-->", "uses": "..>"}


def _mermaid(s: str) -> str:
    return re.sub(r'[~_*{}"<>]', lambda m: _ENTITIES[m.group()], str(s))


def mermaid_text(
    model: Model,
    classes: Sequence[Class],
    relations: Sequence[Relation],
    settings: Settings,
) -> str:
    """The Mermaid text of what is drawn, the same as the web page's Copy gives."""
    out = ["classDiagram", "direction TB"]
    for area, inside in _by_area(classes).items():
        namespace = re.sub(r"[^\w.]+", "_", short(model, area))
        out.append(f"namespace {namespace} {{")
        for c in inside:
            out.append(f'  class {c["id"]}["{c["name"]}"] {{')
            if c["kind"] != "class":
                out.append(f"    <<{c['kind']}>>")
            for line in members(c, settings):
                out.append(f"    {_mermaid(line.text)}{'$' if line.static else ''}")
            out.append("  }")
        out.append("}")
    for r in relations:
        # inherits and implements point from the general to the special in Mermaid
        a, b = _ends(r)
        label = f" : {_mermaid(r['label'])}" if r.get("label") else ""
        out.append(f"{a} {_ARROWS[r['kind']]} {b}{label}")
    return "\n".join(out)


def _by_area(classes: Iterable[Class]) -> dict[str, list[Class]]:
    areas: dict[str, list[Class]] = {}
    for c in classes:
        areas.setdefault(c["area"], []).append(c)
    return areas


def _ends(r: Relation) -> tuple[str, str]:
    """The relation's ends with the general one first: it is drawn above."""
    if r["kind"] in ("inherits", "implements"):
        return r["to"], r["from"]
    return r["from"], r["to"]


# -- the Graphviz layout ----------------------------------------------------------------


@dataclass(frozen=True)
class Box:
    """A rectangle in the drawing's points, y downwards."""

    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Edge:
    """An arrow as laid out: its relation, and a cubic Bézier path from the general end."""

    relation: Relation
    points: tuple[tuple[float, float], ...]  # 1 + 3n control points
    label_at: tuple[float, float] | None


@dataclass(frozen=True)
class Layout:
    """Where everything goes; computed by Graphviz, drawn by the window."""

    width: float
    height: float
    nodes: dict[str, Box]
    edges: list[Edge]
    areas: dict[str, Box]  # area name -> its frame


def dot_text(
    model: Model,
    classes: Sequence[Class],
    relations: Sequence[Relation],
    sizes: Mapping[str, tuple[float, float]],
    texts: Mapping[str, tuple[float, float]] | None = None,
) -> str:
    """The Graphviz input: each area a cluster, each class a box of the size it is drawn.

    Graphviz only keeps room for the areas' titles and the arrows' labels; the window
    writes them, so their sizes are the window's (an empty label of that size).

    Args:
        model: The diagram's model.
        classes: The classes drawn.
        relations: The arrows drawn.
        sizes: Each class's box, in points, as the window will draw it.
        texts: The size of each title and label, by its text, in points.

    Returns:
        The text for ``dot``; the arrows are written in the order of ``relations``.
    """
    texts = texts or {}

    def room(text: str) -> str:
        w, h = texts.get(text, (0.0, 0.0))
        return (
            f'<<TABLE BORDER="0" CELLPADDING="0" CELLSPACING="0" FIXEDSIZE="TRUE" '
            f'WIDTH="{max(1, math.ceil(w))}" HEIGHT="{max(1, math.ceil(h))}">'
            "<TR><TD></TD></TR></TABLE>>"
        )

    out = [
        "digraph classes {",
        "  graph [rankdir=TB, nodesep=0.35, ranksep=0.55, pad=0.2, labeljust=l];",
        '  node [shape=box, fixedsize=true, label=""];',
        "  edge [dir=none, fontsize=10];",
    ]
    for number, (area, inside) in enumerate(_by_area(classes).items()):
        out.append(f"  subgraph cluster_{number} {{")
        out.append(f"    graph [label={room(short(model, area))}, id={_quote(area)}];")
        for c in inside:
            w, h = sizes[c["id"]]
            out.append(
                f"    {c['id']} [width={w / POINTS_PER_INCH:.3f}, "
                f"height={h / POINTS_PER_INCH:.3f}];"
            )
        out.append("  }")
    for r in relations:
        a, b = _ends(r)
        label = f" [label={room(r['label'])}]" if r.get("label") else ""
        out.append(f"  {a} -> {b}{label};")
    out.append("}")
    return "\n".join(out)


def _quote(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_dot(text: str, dot: str = "dot") -> str:
    """Lay out ``text`` with Graphviz; its JSON output, for ``parse_layout``.

    Raises:
        RuntimeError: Graphviz failed.
    """
    done = subprocess.run(  # noqa: S603  (Graphviz, with our own text)
        [dot, "-Tjson"], input=text, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"dot exited {done.returncode}")
    return done.stdout


def parse_layout(output: str, relations: Sequence[Relation]) -> Layout:
    """Read Graphviz's JSON output; y is turned to point downwards.

    Args:
        output: What ``dot -Tjson`` wrote for the text of ``dot_text``.
        relations: The relations given to ``dot_text``, in the same order.
    """
    graph = json.loads(output)
    _, _, width, height = (float(v) for v in graph["bb"].split(","))

    def point(x: float, y: float) -> tuple[float, float]:
        return x, height - y

    nodes: dict[str, Box] = {}
    areas: dict[str, Box] = {}
    for obj in graph.get("objects", []):
        if "bb" in obj:  # a cluster
            x0, y0, x1, y1 = (float(v) for v in obj["bb"].split(","))
            areas[obj.get("id", obj["name"])] = Box(x0, height - y1, x1 - x0, y1 - y0)
        elif "pos" in obj:
            cx, cy = (float(v) for v in obj["pos"].split(","))
            w = float(obj["width"]) * POINTS_PER_INCH
            h = float(obj["height"]) * POINTS_PER_INCH
            x, y = point(cx, cy)
            nodes[obj["name"]] = Box(x - w / 2, y - h / 2, w, h)
    edges = []
    for relation, edge in zip(relations, graph.get("edges", []), strict=True):
        points = tuple(
            point(*(float(v) for v in part.split(",")))
            for part in edge["pos"].split()
            if not part.startswith(("s,", "e,"))
        )
        label_at = None
        if "lp" in edge:
            label_at = point(*(float(v) for v in edge["lp"].split(",")))
        edges.append(Edge(relation, points, label_at))
    return Layout(width, height, nodes, edges, areas)
