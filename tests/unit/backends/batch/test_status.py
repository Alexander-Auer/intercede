"""What ``get_status`` does, on every transport."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from intercede.exceptions import InterCEdeError, ResourceUnavailableError
from intercede.models import JobHandle, JobID, JobStatus, SubmissionSpec

from ._fake_slurm import FakeSlurm

Script = Callable[..., str]


async def submitted(backend: Any, body: str, copies: int = 1) -> list[JobHandle]:
    submission = await backend.submit(SubmissionSpec(executable=body), copies=copies)
    return list(submission.handles.values())


async def test_the_queue_says_how_jobs_are_doing(
    backend: Any, slurm: FakeSlurm, job_script: Script
):
    done = await submitted(backend, job_script("true"))
    failed = await submitted(backend, job_script("exit 3"))
    slurm.hold()
    waiting = await submitted(backend, job_script("true"), copies=2)
    slurm.run(waiting[1].id)

    statuses = await backend.get_status([*done, *failed, *waiting])

    assert list(statuses.values()) == [
        JobStatus.DONE,
        JobStatus.FAILED,
        JobStatus.WAITING,
        JobStatus.RUNNING,
    ]
    assert not [call for call in slurm.calls if call[0] == "sacct"]


async def test_the_history_knows_what_the_queue_forgot(
    backend: Any, slurm: FakeSlurm, job_script: Script
):
    done = await submitted(backend, job_script("true"))
    failed = await submitted(backend, job_script("exit 3"))
    slurm.forget()

    statuses = await backend.get_status([*done, *failed])

    assert list(statuses.values()) == [JobStatus.DONE, JobStatus.FAILED]


async def test_a_job_nobody_knows_is_unknown(backend: Any, job_script: Script):
    known = (await submitted(backend, job_script()))[0]
    unknown = known.model_copy(update={"id": JobID("999_0")})

    statuses = await backend.get_status([known, unknown])

    assert statuses == {known: JobStatus.DONE, unknown: JobStatus.UNKNOWN}


@pytest.mark.parametrize(
    "change",
    [
        {"resource": "elsewhere.example.org"},
        {"backend": "htcondor-ce"},
        {"id": JobID("-j")},
        {"id": JobID("999.0")},
        {"routing": {}},
        {"routing": {"submission": "../../..", "copy": "0"}},
        {"routing": {"submission": "0" * 32, "copy": "../.."}},
    ],
    ids=[
        "resource",
        "backend",
        "option",
        "not-a-slurm-id",
        "no-routing",
        "submission",
        "copy",
    ],
)
async def test_a_handle_that_is_not_ours_is_unknown_and_never_asked_about(
    backend: Any, slurm: FakeSlurm, job_script: Script, change: dict[str, Any]
):
    handle = (await submitted(backend, job_script()))[0]
    theirs = handle.model_copy(update=change)
    before = len(slurm.calls)

    assert await backend.get_status([theirs]) == {theirs: JobStatus.UNKNOWN}
    assert len(slurm.calls) == before


async def test_many_jobs_are_asked_about_in_bounded_commands(
    backend: Any, slurm: FakeSlurm, job_script: Script
):
    handles = await submitted(backend, job_script(), copies=250)
    await backend.get_status(handles)
    queries = [call for call in slurm.calls if call[0] == "squeue"]
    assert len(queries) == 3


async def test_a_controller_that_is_down_fails_the_whole_call(
    backend: Any, slurm: FakeSlurm, job_script: Script
):
    handles = await submitted(backend, job_script())
    slurm.break_tool("squeue", "squeue: error: Unable to contact slurm controller")
    with pytest.raises(ResourceUnavailableError, match="Unable to contact"):
        await backend.get_status(handles)


async def test_an_accounting_that_is_down_fails_the_whole_call(
    backend: Any, slurm: FakeSlurm, job_script: Script
):
    handles = await submitted(backend, job_script())
    slurm.forget()
    slurm.break_tool("sacct", "sacct: error: slurmdbd: Connection refused")
    with pytest.raises(ResourceUnavailableError, match="Connection refused"):
        await backend.get_status(handles)


async def test_a_site_without_accounting_no_longer_knows_finished_jobs(
    backend: Any, slurm: FakeSlurm, job_script: Script
):
    handles = await submitted(backend, job_script())
    slurm.forget()
    slurm.break_tool("sacct", "Slurm accounting storage is disabled")
    assert await backend.get_status(handles) == {handles[0]: JobStatus.UNKNOWN}


async def test_a_closed_backend_refuses(backend: Any, job_script: Script):
    handles = await submitted(backend, job_script())
    await backend.__aexit__(None, None, None)
    with pytest.raises(InterCEdeError, match="closed"):
        await backend.get_status(handles)
    await backend.__aexit__(None, None, None)  # closing twice is harmless
