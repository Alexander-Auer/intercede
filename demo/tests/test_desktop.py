"""The desktop class diagram's data: what is drawn, the Mermaid text, and the Graphviz layout.

The window itself is tested in ``test_desktop_window.py``, where Qt is installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from demo.desktop import diagram as d

DEMO = Path(__file__).resolve().parents[1]


def _class(cid: str, name: str, area: str, kind: str = "class", **extra) -> dict:
    return {
        "id": cid,
        "name": name,
        "area": area,
        "kind": kind,
        "module": area,
        "fields": extra.get("fields", []),
        "methods": extra.get("methods", []),
    }


@pytest.fixture
def model() -> dict:
    return {
        "package": "pkg",
        "areas": [{"name": "pkg.a", "doc": ""}, {"name": "pkg.b", "doc": ""}],
        "classes": [
            _class("c0", "Base", "pkg.a", "protocol"),
            _class(
                "c1",
                "Impl",
                "pkg.a",
                fields=[
                    {"name": "x", "type": "int", "private": False},
                    {"name": "_y", "type": "", "private": True},
                ],
                methods=[
                    {
                        "name": "run",
                        "private": False,
                        "async": True,
                        "static": False,
                        "parameters": ["a"],
                        "returns": "None",
                    },
                    {
                        "name": "make",
                        "private": False,
                        "async": False,
                        "static": True,
                        "parameters": [],
                        "returns": "Impl",
                    },
                ],
            ),
            _class("c2", "Part", "pkg.b"),
            _class("c3", "Oops", "pkg.b", "exception"),
        ],
        "relations": [
            {"kind": "implements", "from": "c1", "to": "c0"},
            {"kind": "holds", "from": "c1", "to": "c2", "label": "_part"},
            {"kind": "uses", "from": "c2", "to": "c3"},
        ],
    }


def test_filters_pick_classes_as_the_web_page_does(model):
    settings = d.Settings()
    assert [c["id"] for c in d.shown_classes(model, settings, None)] == [
        "c0",
        "c1",
        "c2",
        "c3",
    ]
    settings.exceptions = False
    settings.hidden = {"pkg.a"}
    assert [c["id"] for c in d.shown_classes(model, settings, None)] == ["c2"]
    # a focus ignores the filters, and "uses" counts only when shown
    assert [c["id"] for c in d.shown_classes(model, d.Settings(), "c2")] == ["c1", "c2"]
    assert [c["id"] for c in d.shown_classes(model, d.Settings(uses=True), "c2")] == [
        "c1",
        "c2",
        "c3",
    ]
    ids = {"c1", "c2", "c3"}
    assert [r["kind"] for r in d.shown_relations(model, ids, d.Settings())] == ["holds"]
    assert len(d.shown_relations(model, ids, d.Settings(uses=True))) == 2


def test_members_are_written_as_python_does(model):
    impl = model["classes"][1]
    assert d.members(impl, d.Settings()) == []
    lines = d.members(impl, d.Settings(members=True))
    assert [m.text for m in lines] == ["+x: int", "+async run(a)", "+make() Impl"]
    assert [m.static for m in lines] == [False, False, True]
    assert "-_y" in [
        m.text for m in d.members(impl, d.Settings(members=True, private=True))
    ]


def test_the_mermaid_text_is_the_web_pages(model):
    settings = d.Settings(members=True)
    classes = d.shown_classes(model, settings, None)
    relations = d.shown_relations(model, {c["id"] for c in classes}, settings)
    text = d.mermaid_text(model, classes, relations, settings)
    assert text.splitlines()[:4] == [
        "classDiagram",
        "direction TB",
        "namespace a {",
        '  class c0["Base"] {',
    ]
    assert "    <<protocol>>" in text
    assert "    +make() Impl$" in text  # static, as Mermaid marks it
    assert "c0 <|.. c1" in text  # the general end first
    assert "c1 --> c2 : #95;part" in text  # Mermaid's syntax characters as entities


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz is not installed")
def test_graphviz_places_boxes_of_the_size_they_are_drawn(model):
    settings = d.Settings()
    classes = d.shown_classes(model, settings, None)
    relations = d.shown_relations(model, {c["id"] for c in classes}, settings)
    sizes = {c["id"]: (100.0 + 10 * i, 40.0) for i, c in enumerate(classes)}
    text = d.dot_text(
        model, classes, relations, sizes, {"a": (30, 16), "_part": (40, 14)}
    )
    layout = d.parse_layout(d.run_dot(text), relations)

    for cid, (w, h) in sizes.items():
        box = layout.nodes[cid]
        assert (round(box.w, 1), round(box.h, 1)) == (w, h)
        assert 0 <= box.x and box.x + box.w <= layout.width
    assert [e.relation for e in layout.edges] == relations
    assert set(layout.areas) == {"pkg.a", "pkg.b"}
    # y points down: the protocol is above the class that implements it
    assert layout.nodes["c0"].y < layout.nodes["c1"].y
    holds = layout.edges[1]
    assert holds.label_at is not None and len(holds.points) % 3 == 1


def test_the_model_is_the_same_each_time_the_code_is_read():
    """The window keeps layouts by their input: the same code must give the same model."""
    pytest.importorskip("intercede")  # read by the checkout's Python, not the app's
    read = [
        subprocess.run(  # noqa: S603  (constant input)
            [
                sys.executable,
                str(DEMO / "desktop" / "extract.py"),
                str(DEMO / "classes.py"),
            ],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        for _ in range(2)
    ]
    assert read[0] == read[1]
    assert json.loads(read[0])["sources"]


SOURCE = """import functools


class Thing:
    @functools.cache
    async def long(
        self,
        a: int,
    ) -> int:
        def inner() -> int:
            return a

        return inner()

    def after(self) -> None:
        pass
"""


def test_a_function_is_found_whole_from_its_first_line():
    text, first = d.function_source(
        SOURCE, 5
    )  # the decorator's line, as Python records it
    assert first == 5
    assert text.splitlines()[0].strip() == "@functools.cache"
    assert (
        text.splitlines()[-1].strip() == "return inner()"
    )  # past the signature's lines
    inner, _ = d.function_source(SOURCE, 10)
    assert inner.splitlines() == ["        def inner() -> int:", "            return a"]


def _node(number, parent, qualname, module="pkg.a", kind="call"):
    return {
        "id": number,
        "parent": parent,
        "kind": kind,
        "qualname": qualname,
        "module": module,
        "step_in": number,
    }


def test_a_traced_path_numbers_the_classes_and_names_the_calls(model):
    nodes = [
        _node(0, None, "Impl.run"),
        _node(1, 0, "helper"),  # a module function: its calls are Impl's
        _node(2, 1, "Part.make", module="pkg.b"),
        _node(3, 2, "Part.check", module="pkg.b"),  # within one class: no arrow
        _node(4, 0, "Part.save", module="pkg.b"),
        _node(5, 0, "sbatch", kind="command"),
    ]
    order, calls = d.trace_path(model, nodes)
    assert order == {"c1": 1, "c2": 2}
    assert calls == [{"kind": "calls", "from": "c1", "to": "c2", "label": "make, save"}]


needs_monitoring = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="tracing needs sys.monitoring"
)


@needs_monitoring
def test_a_call_is_followed_through_the_backend_and_back():
    """The fake needs no resource, so it always runs; the stacks are traced the same way."""
    pytest.importorskip("intercede")

    def trace(*args: str) -> dict:
        done = subprocess.run(  # noqa: S603  (constant input)
            [sys.executable, str(DEMO / "desktop" / "trace.py"), *args],
            capture_output=True,
            check=True,
            text=True,
            cwd=DEMO.parent,
        )
        return json.loads(done.stdout)

    listed = {b["name"]: b for b in trace("--list")["backends"]}
    assert listed["fake"]["running"] is True
    assert {"submit", "get_status"} <= set(listed["fake"]["operations"])
    run = trace("fake", "submit")
    nodes = {n["id"]: n for n in run["nodes"]}
    roots = [n for n in nodes.values() if n["parent"] is None]
    assert [n["name"] for n in roots] == [f"{run['class']}.submit"]
    assert all("end" in n for n in nodes.values())  # every call returned
    assert all(n["step_in"] < n["step_out"] for n in nodes.values())
    assert run["result"].startswith("Submission(")


@needs_monitoring
def test_what_leaves_the_process_is_recorded_under_the_call_that_sent_it(
    tmp_path, monkeypatch
):
    import asyncio
    import importlib.util

    from demo.desktop import trace

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", asyncio.create_subprocess_exec
    )
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "work.py").write_text(
        "import asyncio\n\n"
        "async def run():\n"
        "    process = await asyncio.create_subprocess_exec(\n"
        "        'echo', 'hi', stdout=asyncio.subprocess.PIPE\n"
        "    )\n"
        "    out, _ = await process.communicate()\n"
        "    return out\n"
    )
    spec = importlib.util.spec_from_file_location("pkg_work", package / "work.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    recorder = trace.Recorder(package)
    trace.watch_processes(recorder)
    with recorder:
        asyncio.run(module.run())

    run, command = recorder.nodes
    assert (run["name"], run["returned"]) == ("run", "b'hi\\n'")
    assert command["kind"] == "command" and command["name"] == "echo hi"
    assert command["parent"] == run["id"] and command["call_line"] == 4
    assert command["returned"] == "exit 0: hi"
