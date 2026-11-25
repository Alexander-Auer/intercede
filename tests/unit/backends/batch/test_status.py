"""What ``get_status`` does that a Slurm stack does not show, on every transport.

Jobs that finish, fail, or are forgotten by the controller are the shared
suite's and ``test_slurm.py``'s, on the Slurm stacks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from intercede.exceptions import ResourceUnavailableError
from intercede.models import JobHandle, JobID, JobStatus, SubmissionSpec

from ._fake_slurm import FakeSlurm

Script = Callable[..., str]


async def submitted(backend: Any, body: str, copies: int = 1) -> list[JobHandle]:
    submission = await backend.submit(SubmissionSpec(executable=body), copies=copies)
    return list(submission.handles.values())


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("PENDING", JobStatus.WAITING),
        ("SUSPENDED", JobStatus.WAITING),
        ("CONFIGURING", JobStatus.WAITING),
        ("RUNNING", JobStatus.RUNNING),
        ("COMPLETING", JobStatus.RUNNING),
        ("COMPLETED", JobStatus.DONE),
        ("FAILED", JobStatus.FAILED),
        ("TIMEOUT", JobStatus.FAILED),
        ("OUT_OF_MEMORY", JobStatus.FAILED),
        ("CANCELLED", JobStatus.ABORTED),
        ("PREEMPTED", JobStatus.ABORTED),
        ("NODE_FAIL", JobStatus.ABORTED),
        ("SOMETHING_NEW", JobStatus.UNKNOWN),
    ],
)
async def test_the_states_slurm_reports(
    backend: Any,
    slurm: FakeSlurm,
    job_script: Script,
    state: str,
    expected: JobStatus,
):
    slurm.hold()
    handle = (await submitted(backend, job_script()))[0]
    slurm.report(handle.id, state)
    assert await backend.get_status([handle]) == {handle: expected}


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
