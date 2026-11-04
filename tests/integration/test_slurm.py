"""What only Slurm does, on the stacks with Slurm. Everything else is the shared suite."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from intercede._backends.batch.protocols import Transport
from intercede.models import JobStatus, Resources, SubmissionSpec
from intercede.protocols import JobBackend, Killable
from tests.integration.jobs import (
    FINISH_TIMEOUT_S,
    POLL_INTERVAL_S,
    release,
    script,
    wait_for,
)

pytestmark = [pytest.mark.scheduler, pytest.mark.slurm]

# More than the node has (slurm.conf), so a job asking for this waits until it is cancelled.
TOO_MANY_CPUS = Resources(cpus=99)


async def test_copies_are_the_tasks_of_one_array(backend: JobBackend, tmp_path: Path):
    submission = await backend.submit(
        SubmissionSpec(executable=script(tmp_path, "true")), copies=3
    )
    handles = list(submission.handles.values())
    try:
        array = handles[0].id.partition("_")[0]
        assert [handle.id for handle in handles] == [f"{array}_{i}" for i in range(3)]
    finally:
        await release(backend, handles)


async def test_a_job_the_controller_forgot_keeps_its_result(
    backend: JobBackend, transport: Transport, tmp_path: Path
):
    good = await backend.submit(SubmissionSpec(executable=script(tmp_path, "true")))
    bad = await backend.submit(SubmissionSpec(executable=script(tmp_path, "exit 3")))
    handles = [good.handles[0], bad.handles[0]]
    expected = {handles[0]: JobStatus.DONE, handles[1]: JobStatus.FAILED}
    try:
        assert (
            await wait_for(backend, handles, frozenset(expected.values())) == expected
        )

        # MinJobAge in slurm.conf, and a controller that only cleans up now and then.
        ids = ",".join(handle.id for handle in handles)
        deadline = time.monotonic() + FINISH_TIMEOUT_S
        while True:
            queue = await transport.run(
                ["squeue", "-h", "-r", "-t", "all", "-j", ids, "-o", "%i"], timeout=30
            )
            if not queue.stdout.strip():
                break
            assert time.monotonic() < deadline, f"squeue never forgot: {queue.stdout}"
            await asyncio.sleep(POLL_INTERVAL_S)

        assert await backend.get_status(handles) == expected
    finally:
        await release(backend, handles)


@pytest.mark.parametrize("copies", [1, 3], ids=["alone", "beside-others"])
async def test_a_job_cancelled_while_it_waits_ends_up_aborted(
    backend: JobBackend, killable: Killable, tmp_path: Path, copies: int
):
    """Slurm drops such a task from its array: no row in the queue or the accounting."""
    spec = SubmissionSpec(executable=script(tmp_path, "true"), resources=TOO_MANY_CPUS)
    submission = await backend.submit(spec, copies=copies)
    handles = list(submission.handles.values())
    try:
        await wait_for(backend, handles, frozenset({JobStatus.WAITING}))

        outcomes = await killable.kill(handles[:1])

        assert outcomes[handles[0]].ok, outcomes[handles[0]].reason
        await wait_for(backend, handles[:1], frozenset({JobStatus.ABORTED}))
        statuses = await backend.get_status(handles[1:])
        assert set(statuses.values()) <= {JobStatus.WAITING}
    finally:
        await killable.kill(handles)
        await release(backend, handles)
