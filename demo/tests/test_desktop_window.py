"""The desktop class diagram's window, offscreen; it needs Qt, so it runs in the app's own environment.

``pixi run --manifest-path demo/desktop/pixi.toml pytest demo/tests/test_desktop_window.py``
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from demo.desktop.window import Window, checkout_python  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    os.environ["XDG_CACHE_HOME"] = str(tmp_path_factory.mktemp("cache"))
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path_factory.mktemp("config"))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("intercede-test")
    app.setApplicationName("classes-window-test")
    return app


def _until(app, condition, timeout=30.0):
    end = time.monotonic() + timeout
    while not condition():
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if time.monotonic() > end:
            pytest.fail("the window did not get there in time")


@pytest.fixture
def window(app):
    dot = str(Path(sys.prefix) / "bin" / "dot")
    window = Window(
        REPO,
        checkout_python(REPO),
        dot if os.access(dot, os.X_OK) else shutil.which("dot"),
        None,
    )
    window.resize(1400, 900)
    window.show()
    _until(app, lambda: window.items and window.reader.process is None)
    yield window
    window.close()


def test_every_class_the_code_has_is_drawn(window):
    assert window.model is not None
    assert len(window.items) == len(window.model["classes"])
    assert "arrows" in window.canvas.status.text()


def test_a_class_not_drawn_is_shown_with_its_neighbours(app, window):
    window.switches["exceptions"].setChecked(False)
    _until(app, lambda: all(i.kind != "exception" for i in window.items.values()))
    error = next(c for c in window.model["classes"] if c["kind"] == "exception")
    window.go(error["id"])
    _until(app, lambda: window.chosen == error["id"])
    assert window.focus == error["id"] and error["id"] in window.items
    assert window.detail.isVisible() and window.detail_name.text() == error["name"]
    window.unfocus()
    window.switches["exceptions"].setChecked(True)
    _until(app, lambda: len(window.items) == len(window.model["classes"]))


def test_search_marks_what_it_finds(window):
    window.search.setText("Backend")
    window.searched("Backend")
    found = [i for i in window.items.values() if i.found]
    assert found and all("backend" in i.name.lower() for i in found)
    assert all(i.opacity() < 1 for i in window.items.values() if not i.found)
    window.search.clear()
    window.mark()
