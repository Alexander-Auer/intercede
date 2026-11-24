"""Shared suite: Diagnosable, on every stack whose backend has it."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from intercede.models import Diagnostics, JobHandle, JobStatus, SubmissionSpec
from intercede.protocols import Diagnosable, JobBackend
from tests.integration.jobs import release, script, wait_for

pytestmark = pytest.mark.contract

# A job reported running may not have written its first line yet.
WRITTEN_TIMEOUT_S = 60


async def read_until(
    diagnosable: Diagnosable, handle: JobHandle, filename: str | None, text: str
) -> Diagnostics:
    """Read a file of a running job until it holds ``text``; fail after the timeout."""
    deadline = time.monotonic() + WRITTEN_TIMEOUT_S
    while True:
        outcome = (await diagnosable.get_diagnostics([handle], filename))[handle]
        if outcome.ok and outcome.value is not None and text in outcome.value.content:
            return outcome.value
        if time.monotonic() > deadline:
            pytest.fail(f"{filename or 'stdout'} never held {text!r}: {outcome}")
        await asyncio.sleep(2)


async def test_a_running_job_can_be_read(
    backend: JobBackend, diagnosable: Diagnosable, tmp_path: Path
):
    submission = await backend.submit(
        SubmissionSpec(
            executable=script(
                tmp_path, "echo started; echo step-1 > job.log; sleep 600"
            )
        )
    )
    handles = list(submission.handles.values())
    try:
        await wait_for(backend, handles, frozenset({JobStatus.RUNNING}))

        stdout = await read_until(diagnosable, handles[0], None, "started")
        log = await read_until(diagnosable, handles[0], "job.log", "step-1")

        assert stdout.content == "started\n"
        assert (log.filename, log.content) == ("job.log", "step-1\n")
        # Reading is repeatable, and changes nothing: the job runs on.
        again = (await diagnosable.get_diagnostics(handles))[handles[0]]
        assert again.value == stdout
        assert (await backend.get_status(handles))[handles[0]] == JobStatus.RUNNING
    finally:
        await release(backend, handles)


async def test_a_file_the_job_has_not_written(
    backend: JobBackend, diagnosable: Diagnosable, tmp_path: Path
):
    submission = await backend.submit(
        SubmissionSpec(executable=script(tmp_path, "sleep 600"))
    )
    handles = list(submission.handles.values())
    try:
        await wait_for(backend, handles, frozenset({JobStatus.RUNNING}))

        outcome = (await diagnosable.get_diagnostics(handles, "missing.log"))[
            handles[0]
        ]

        assert not outcome.ok and outcome.reason
    finally:
        await release(backend, handles)
