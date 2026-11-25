"""Work done away from the window: reading the code, and laying it out, in processes of their own.

The model is read by the checkout's own Python (``extract.py``), so it is that
branch's code, imported afresh. Graphviz lays it out; each layout is kept, in memory
and on disk, keyed by its input, so a view seen before needs no layout at all.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    QProcess,
    QProcessEnvironment,
    QStandardPaths,
    Signal,
)

from . import diagram as d

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


def checkout_environment(python: list[str]) -> QProcessEnvironment:
    """The environment the checkout's Python runs in, as ``pixi run`` would give it.

    Its ``bin`` folder comes first on ``PATH``: the tools a backend runs (installed
    in that environment) are found as they are under ``pixi run``. The app's own
    ``PYTHONPATH`` and ``PYTHONHOME`` are not passed on.
    """
    environment = QProcessEnvironment.systemEnvironment()
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.remove(name)
    folder = Path(python[0]).parent
    if folder.name == "bin" and (folder / "python").exists():
        path = environment.value("PATH", "")
        environment.insert(
            "PATH", f"{folder}{os.pathsep}{path}" if path else str(folder)
        )
        environment.insert("CONDA_PREFIX", str(folder.parent))
    return environment


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
        process.setProcessEnvironment(checkout_environment(self.python))
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


class Tracer(QObject):
    """Records calls with the checkout's Python (``trace.py``), one at a time."""

    listed = Signal(list)  # the backends there are, each with its operations
    traced = Signal(dict)  # one call, recorded
    failed = Signal(str)
    progress = Signal(str)  # what it is doing: waiting for a job, recording

    def __init__(self, repo: Path, python: list[str]) -> None:
        super().__init__()
        self.repo = repo
        self.python = python
        self.process: QProcess | None = None

    def list(self) -> None:
        self._start(["--list"], lambda data: self.listed.emit(data["backends"]))

    def run(self, backend: str, operation: str) -> None:
        self._start([backend, operation], self.traced.emit)

    def stop(self) -> None:
        if self.process is not None:
            self.process.finished.disconnect()
            self.process.kill()
            self.process.waitForFinished(1000)
            self.process.deleteLater()
            self.process = None

    def _start(self, args: list[str], done: Any) -> None:
        self.stop()  # a newer request replaces one still running
        process = QProcess(self)
        process.setProcessEnvironment(checkout_environment(self.python))
        process.setWorkingDirectory(str(self.repo))
        process.finished.connect(lambda *_: self._done(process, done))
        process.readyReadStandardError.connect(lambda: self._said(process))
        self.process = process
        command = [*self.python, str(HERE / "trace.py"), *args]
        process.start(command[0], command[1:])

    def _said(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardError().data()).decode(errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        if lines:
            self.progress.emit(lines[-1])
            self.last_said = lines[-1]

    def _done(self, process: QProcess, done: Any) -> None:
        if self.process is process:
            self.process = None
        out = bytes(process.readAllStandardOutput().data())
        process.deleteLater()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            self.failed.emit(
                getattr(self, "last_said", "") or "the trace could not be run"
            )
            return
        if "error" in data and "nodes" not in data:
            self.failed.emit(str(data["error"]).strip().splitlines()[-1])
            return
        done(data)
