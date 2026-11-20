"""The ARC backend: status."""

from __future__ import annotations

import pytest

from intercede import JobHandle, JobID, JobStatus
from intercede._backends.monolith.arc import ARCBackend

from ._fake_arc import ENDPOINT, SPEC, FakeARC


class TestStatus:
    async def test_the_states_ce_reports(self, ce: ARCBackend, arc: FakeARC):
        submission = await ce.submit(SPEC, 4)
        handles = list(submission.handles.values())
        for job, state in zip(
            arc.created(), ["QUEUING", "RUNNING", "FINISHED", "FAILED"]
        ):
            job.state = state
        assert list((await ce.get_status(handles)).values()) == [
            JobStatus.WAITING,
            JobStatus.RUNNING,
            JobStatus.DONE,
            JobStatus.FAILED,
        ]

    async def test_a_job_the_ce_does_not_know_is_unknown(
        self, ce: ARCBackend, arc: FakeARC
    ):
        handle = (await ce.submit(SPEC)).handles[0]
        gone = handle.model_copy(update={"id": JobID("0123abcd")})
        statuses = await ce.get_status([handle, gone])
        assert statuses == {handle: JobStatus.WAITING, gone: JobStatus.UNKNOWN}

    async def test_each_job_is_asked_about_once(self, ce: ARCBackend, arc: FakeARC):
        handle = (await ce.submit(SPEC)).handles[0]
        arc.requests.clear()
        await ce.get_status([handle, handle])
        (request,) = arc.requests
        assert request.body.count(handle.id.encode()) == 1

    @pytest.mark.parametrize(
        "foreign",
        [
            {"resource": "https://other.example.org/arex"},
            {"backend": "htcondor-ce"},
            {"id": "../../info"},
            {"id": "1.0"},
            {"id": ""},
        ],
        ids=["other-ce", "other-backend", "path", "htcondor-id", "empty"],
    )
    async def test_foreign_and_malformed_handles_never_reach_a_request(
        self, ce: ARCBackend, arc: FakeARC, foreign: dict[str, str]
    ):
        handle = JobHandle(backend="arc", resource=ENDPOINT, id=JobID("0123abcd"))
        theirs = handle.model_copy(update=foreign)
        assert await ce.get_status([theirs]) == {theirs: JobStatus.UNKNOWN}
        assert arc.requests == []
