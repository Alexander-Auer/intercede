"""``condor_submit``, ``condor_q``, ``condor_history`` and ``condor_rm``, for the composed backend's tests.

Built like ``_fake_slurm.py``: the fixture puts a small script on ``PATH`` per tool,
which runs this file as a program with that tool's name and keeps its state in
files; the tests import ``FakeSchedd`` to look at that state and change it.

A job moves its own files as HTCondor moves them, which is what the tests check:
``condor_submit`` runs each process in an empty scratch directory that holds only
what ``transfer_input_files`` names, at the same relative paths
(``preserve_relative_paths``), and copies what ``transfer_output_files`` names
back to ``initialdir`` when it ends. Without ``preserve_relative_paths`` a path
arrives flattened to its last name, as HTCondor does. A job runs to its end during
``condor_submit``, unless the schedd was told to ``hold`` them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
from pathlib import Path
from typing import Any

STATE_VARIABLE = "FAKE_SCHEDD_STATE"
TOOLS = ("condor_submit", "condor_q", "condor_history", "condor_rm")
IDLE, RUNNING, REMOVED, COMPLETED, HELD = 1, 2, 3, 4, 5


class FakeSchedd:
    """The schedd's state, as the tests see it."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def calls(self) -> list[list[str]]:
        """Every tool run so far, as its argv."""
        return _load(self._directory).get("calls", [])

    @property
    def descriptions(self) -> list[str]:
        """Every submit description submitted, in order."""
        return _load(self._directory).get("descriptions", [])

    def hold(self) -> None:
        """Keep jobs from now on in the queue, idle, until ``run``."""
        self._change(lambda state: state.update(hold=True))

    def run(self, job: str) -> None:
        """Say a waiting job has started."""
        self._change(lambda state: state["queue"][job].update(JobStatus=RUNNING))

    def report(self, job: str, **ad: Any) -> None:
        """Have the queue report these attributes for a job it holds."""
        self._change(lambda state: state["queue"][job].update(ad))

    def _change(self, change: Any) -> None:
        state = _load(self._directory)
        change(state)
        _save(self._directory, state)


def _load(directory: Path) -> dict[str, Any]:
    path = directory / "state.json"
    if not path.exists():
        return {"next": 1, "queue": {}, "history": {}, "calls": [], "descriptions": []}
    return json.loads(path.read_text())


def _save(directory: Path, state: dict[str, Any]) -> None:
    (directory / "state.json").write_text(json.dumps(state))


def _description(text: str) -> tuple[dict[str, str], int]:
    settings: dict[str, str] = {}
    count = 0
    for line in text.splitlines():
        if match := re.fullmatch(r"queue\s+(\d+)", line.strip()):
            count = int(match[1])
        elif "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip().lower()] = value.strip()
    return settings, count


def _expand(value: str, proc: int) -> str:
    return value.replace("$(ProcId)", str(proc)).replace("$(DOLLAR)", "$")


def _arguments(value: str, proc: int) -> list[str]:
    """HTCondor's new syntax: double-quoted, items in single quotes, a quote doubled."""
    inner = _expand(value, proc)[1:-1]
    return [item.replace("''", "'") for item in re.findall(r"'((?:[^']|'')*)'", inner)]


def _files(value: str, proc: int) -> list[str]:
    return [name.strip() for name in _expand(value, proc).split(",") if name.strip()]


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def _run(settings: dict[str, str], proc: int, initialdir: Path) -> int:
    """Run one process as HTCondor would: in scratch, with only the files it names."""
    keep_paths = settings.get("preserve_relative_paths", "false").lower() == "true"
    with tempfile.TemporaryDirectory(prefix="scratch-") as name:
        scratch = Path(name)
        for item in _files(settings.get("transfer_input_files", ""), proc):
            _copy(
                initialdir / item, scratch / (item if keep_paths else Path(item).name)
            )
        log = initialdir / _expand(settings["output"], proc)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as output:
            finished = subprocess.run(  # noqa: S603
                [settings["executable"], *_arguments(settings["arguments"], proc)],
                cwd=scratch,
                env={"PATH": os.environ["PATH"]},  # not the submitter's environment
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        for item in _files(settings.get("transfer_output_files", ""), proc):
            if (scratch / item).exists():
                _copy(scratch / item, initialdir / item)
    return finished.returncode


def _submit(state: dict[str, Any], argv: list[str]) -> int:
    text = Path(argv[-1]).read_text()
    settings, count = _description(text)
    initialdir = Path(_expand(settings["initialdir"], 0))
    for proc in range(count):
        for item in _files(settings.get("transfer_input_files", ""), proc):
            if not (initialdir / item).exists():
                print(f'ERROR: Can\'t open "{initialdir / item}"', file=sys.stderr)
                return 1
    state["descriptions"].append(text)
    cluster = int(state["next"])
    state["next"] = cluster + 1
    for proc in range(count):
        job = f"{cluster}.{proc}"
        ad = {"ClusterId": cluster, "ProcId": proc}
        if state.get("hold"):
            state["queue"][job] = {**ad, "JobStatus": IDLE}
            continue
        code = _run(settings, proc, initialdir)
        state["history"][job] = {**ad, "JobStatus": COMPLETED, "ExitCode": code}
    print(f"{cluster}.0 - {cluster}.{count - 1}")
    return 0


def _ids(argv: list[str]) -> list[str]:
    return [arg for arg in argv if re.fullmatch(r"\d+\.\d+", arg)]


def _query(state: dict[str, Any], argv: list[str]) -> int:
    ads = [state["queue"][job] for job in _ids(argv) if job in state["queue"]]
    if ads:  # condor_q prints nothing at all when no job matches
        print(json.dumps(ads))
    return 0


def _history(state: dict[str, Any], argv: list[str]) -> int:
    print(
        json.dumps(
            [state["history"][job] for job in _ids(argv) if job in state["history"]]
        )
    )
    return 0


def _remove(state: dict[str, Any], argv: list[str]) -> int:
    missing = False
    for job in _ids(argv):
        ad = state["queue"].pop(job, None)
        if ad is None:
            missing = True
            continue
        state["history"][job] = {
            **ad,
            "JobStatus": REMOVED,
            "LastJobStatus": ad["JobStatus"],
        }
        print(f"Job {job} marked for removal")
    if missing:
        print("Couldn't find/remove all jobs matching constraint", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    tool, *argv = sys.argv[1:]
    directory = Path(os.environ[STATE_VARIABLE])
    state = _load(directory)
    state["calls"].append([tool, *argv])
    code = {
        "condor_submit": _submit,
        "condor_q": _query,
        "condor_history": _history,
        "condor_rm": _remove,
    }
    returncode = code[tool](state, argv)
    _save(directory, state)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
