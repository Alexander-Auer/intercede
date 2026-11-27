"""What the HTCondor scheduler does that its stack does not show, through the composed backend.

The shared suite runs on the HTCondor stack. These check what it cannot: that a job
gets only its own files and needs no file system it shares with the submitting host,
that the copies are one cluster, and the states a schedd seldom reaches there. The
schedd is ``_fake_schedd``, which moves a job's files as HTCondor does.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from intercede import registry
from intercede.auth import StaticCredentials
from intercede.config import BatchLocalConfig
from intercede.exceptions import ConfigurationError, SpecificationRejectedError
from intercede.models import (
    CopyVars,
    FileRef,
    JobStatus,
    OutputMember,
    OutputSpec,
    Resources,
    SubmissionSpec,
)

from . import _fake_schedd


@pytest.fixture
def schedd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _fake_schedd.FakeSchedd:
    """HTCondor's tools on ``PATH``, for this test only."""
    tools = tmp_path / "condor-bin"
    tools.mkdir()
    program = shlex.join([sys.executable, _fake_schedd.__file__])
    for name in _fake_schedd.TOOLS:
        tool = tools / name
        tool.write_text(f'#!/bin/sh\nexec {program} {name} "$@"\n')
        tool.chmod(0o755)
    state = tmp_path / "schedd"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(_fake_schedd.STATE_VARIABLE, str(state))
    return _fake_schedd.FakeSchedd(state)


def config(**fields: Any) -> BatchLocalConfig:
    return BatchLocalConfig(
        kind="batch", transport="local", scheduler="htcondor", **fields
    )


@pytest.fixture
async def backend(home: Path, schedd: _fake_schedd.FakeSchedd) -> AsyncIterator[Any]:
    """The composed backend on HTCondor, on this host, open."""
    async with registry.get_backend(config(), StaticCredentials()) as opened:
        yield opened


def script(folder: Path, body: str) -> str:
    path = folder / "job.sh"
    path.write_text(f"#!/bin/sh\n{body}\n")
    return str(path)


async def test_the_copies_are_one_cluster_and_each_gets_only_its_own_files(
    backend: Any, schedd: _fake_schedd.FakeSchedd, tmp_path: Path
):
    shared = tmp_path / "shared.txt"
    shared.write_text("for everyone")
    spec = SubmissionSpec(
        # Its working directory, and every copy that reached the node.
        executable=script(tmp_path, "echo $(ls -A | sort) / $(ls ../.. | sort)"),
        inputs=[FileRef(name="shared.txt", source=str(shared))],
    )
    copies = [
        CopyVars(secrets={"zero.key": "secret of copy 0"}),
        CopyVars(secrets={"one.key": "secret of copy 1"}),
    ]
    submission = await backend.submit(spec, copies)

    submits = [call for call in schedd.calls if call[0] == "condor_submit"]
    assert len(submits) == 1 and schedd.descriptions[0].endswith("queue 2\n")
    handles = [submission.handles[0], submission.handles[1]]
    outputs = await backend.get_output(handles, tmp_path / "out")
    seen = [outputs[h].value.stdout.read_text().split() for h in handles]  # type: ignore[union-attr]
    assert seen == [
        ["shared.txt", "zero.key", "/", "0"],
        ["one.key", "shared.txt", "/", "1"],
    ]


async def test_what_a_job_writes_on_the_node_comes_back_to_be_collected(
    backend: Any, tmp_path: Path
):
    spec = SubmissionSpec(
        executable=script(tmp_path, "echo done > result.txt; echo payload"),
        outputs=OutputSpec(members=[OutputMember(pattern="result.txt")]),
    )
    handle = (await backend.submit(spec)).handles[0]

    assert (await backend.get_status([handle]))[handle] == JobStatus.DONE
    output = (await backend.get_output([handle], tmp_path / "out"))[handle].value
    assert output is not None
    assert output.stdout.read_text() == "payload\n"
    assert output.files["result.txt"].read_text() == "done\n"


@pytest.mark.parametrize(
    ("ad", "expected"),
    [
        ({}, JobStatus.WAITING),  # idle
        ({"JobStatus": _fake_schedd.RUNNING}, JobStatus.RUNNING),
        # held while its inputs are spooled: on its way
        ({"JobStatus": _fake_schedd.HELD, "HoldReasonCode": 16}, JobStatus.WAITING),
        # held by the site or by HTCondor, say for its wall time: stuck
        ({"JobStatus": _fake_schedd.HELD, "HoldReasonCode": 47}, JobStatus.ABORTED),
    ],
)
async def test_the_states_of_a_job_still_in_the_queue(
    backend: Any,
    schedd: _fake_schedd.FakeSchedd,
    tmp_path: Path,
    ad: dict[str, Any],
    expected: JobStatus,
):
    schedd.hold()
    handle = (
        await backend.submit(SubmissionSpec(executable=script(tmp_path, "true")))
    ).handles[0]
    if ad:
        schedd.report(handle.id, **ad)
    assert (await backend.get_status([handle]))[handle] == expected


async def test_killing_a_waiting_job_removes_it(
    backend: Any, schedd: _fake_schedd.FakeSchedd, tmp_path: Path
):
    schedd.hold()
    handle = (
        await backend.submit(SubmissionSpec(executable=script(tmp_path, "true")))
    ).handles[0]

    assert (await backend.kill([handle]))[handle].ok
    assert ["condor_rm", handle.id] in schedd.calls
    assert (await backend.get_status([handle]))[handle] == JobStatus.ABORTED


async def test_a_queue_is_refused_since_a_schedd_has_none(backend: Any, tmp_path: Path):
    spec = SubmissionSpec(
        executable=script(tmp_path, "true"), resources=Resources(queue="short")
    )
    with pytest.raises(SpecificationRejectedError, match="no queues"):
        await backend.submit(spec)
    with pytest.raises(ConfigurationError, match="no queues"):
        registry.get_backend(config(queue="short"), StaticCredentials())
