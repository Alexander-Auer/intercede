"""``sbatch``, ``squeue``, ``sacct`` and ``scancel``, for the composed backend's tests.

This file is both: the fixture puts a small script on ``PATH`` per tool, which runs
this file as a program with that tool's name and keeps its state in files; and the
tests import ``FakeSlurm`` to look at that state and change it.
It prints what the real tools were seen to print (``_slurm/test_core.py``). A job
runs to its end during ``sbatch``, unless the cluster was told to ``hold`` them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404
import sys
from pathlib import Path
from typing import Any

STATE_VARIABLE = "FAKE_SLURM_STATE"
TOOLS = ("sbatch", "squeue", "sacct", "scancel")


class FakeSlurm:
    """The cluster's state, as the tests see it."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def calls(self) -> list[list[str]]:
        """Every tool run so far, as its argv."""
        return _load(self._directory).get("calls", [])

    def hold(self) -> None:
        """Keep jobs from now on waiting: they stay PENDING until ``run``."""
        self._change(lambda state: state.update(hold=True))

    def run(self, job: str) -> None:
        """Say a held job has started."""
        self._change(lambda state: state["tasks"].update({job: "RUNNING"}))

    def forget(self) -> None:
        """Have the controller forget every job, as it does a while after one ended."""
        self._change(lambda state: state.update(forgotten=True))

    def break_tool(
        self, tool: str, stderr: str = "", *, stdout: str = "", returncode: int = 1
    ) -> None:
        """Make a tool print this and exit with this from now on, doing nothing else."""
        self._change(
            lambda state: state["broken"].update({tool: [returncode, stdout, stderr]})
        )

    def finish_during_cancel(self, job: str) -> None:
        """Have a job end while ``scancel`` fails on it."""
        self._change(lambda state: state.update(finishing=job))

    def _change(self, change: Any) -> None:
        state = _load(self._directory)
        change(state)
        _save(self._directory, state)


def _load(directory: Path) -> dict[str, Any]:
    path = directory / "state.json"
    if not path.exists():
        return {"next": 1, "tasks": {}, "calls": [], "broken": {}}
    return json.loads(path.read_text())


def _save(directory: Path, state: dict[str, Any]) -> None:
    (directory / "state.json").write_text(json.dumps(state))


def _sbatch(state: dict[str, Any], argv: list[str]) -> int:
    script = Path(argv[-1])
    text = script.read_text()
    options = dict(re.findall(r"^#SBATCH --([\w-]+)=(.*)$", text, re.MULTILINE))
    options.update(
        arg[2:].split("=", 1) for arg in argv if arg.startswith("--") and "=" in arg
    )
    array = int(state["next"])
    state["next"] = array + 1
    count = int(options["array"].split("-")[1]) + 1
    for task in range(count):
        job = f"{array}_{task}"
        if state.get("hold"):
            state["tasks"][job] = "PENDING"
            continue
        log = Path(options["chdir"]) / options["output"].replace("%a", str(task))
        with log.open("w") as output:
            finished = subprocess.run(  # noqa: S603
                ["/bin/sh", str(script.resolve())],  # noqa: S607
                cwd=options["chdir"],
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": os.environ["HOME"],
                    "SLURM_ARRAY_TASK_ID": str(task),
                },
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        state["tasks"][job] = "COMPLETED" if finished.returncode == 0 else "FAILED"
    print(array)
    return 0


def _option(argv: list[str], name: str) -> list[str]:
    return argv[argv.index(name) + 1].split(",")


def _squeue(state: dict[str, Any], argv: list[str]) -> int:
    known = {} if state.get("forgotten") else state["tasks"]
    asked = _option(argv, "-j")
    if not any(job in known for job in asked):
        print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
        return 1
    for job in asked:
        if job in known:
            print(f"{job}|{known[job]}")
    return 0


def _sacct(state: dict[str, Any], argv: list[str]) -> int:
    for array in _option(argv, "-j"):
        tasks = {
            job: task_state
            for job, task_state in state["tasks"].items()
            if job.split("_")[0] == array
        }
        for job, task_state in tasks.items():
            shown = "CANCELLED by 1000" if task_state == "CANCELLED" else task_state
            print(f"{job}|{shown}")
        if not tasks and array in state.get("emptied", []):
            print(f"{array}|CANCELLED by 1000")
    return 0


def _scancel(state: dict[str, Any], argv: list[str]) -> int:
    for job in argv:
        if state["tasks"].get(job) == "PENDING":
            # Slurm drops a task from its array that was cancelled while it waited.
            del state["tasks"][job]
            array = job.split("_")[0]
            if not any(known.split("_")[0] == array for known in state["tasks"]):
                state.setdefault("emptied", []).append(array)
        elif state["tasks"].get(job) == "RUNNING":
            state["tasks"][job] = "CANCELLED"
    return 0


def main() -> int:
    tool, *argv = sys.argv[1:]
    directory = Path(os.environ[STATE_VARIABLE])
    state = _load(directory)
    state["calls"].append([tool, *argv])
    if tool in state["broken"]:
        returncode, stdout, stderr = state["broken"][tool]
        print(stdout)
        print(stderr, file=sys.stderr)
        if tool == "scancel" and "finishing" in state:
            state["tasks"][state["finishing"]] = "COMPLETED"
        _save(directory, state)
        return returncode
    code = {"sbatch": _sbatch, "squeue": _squeue, "sacct": _sacct, "scancel": _scancel}
    returncode = code[tool](state, argv)
    _save(directory, state)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
