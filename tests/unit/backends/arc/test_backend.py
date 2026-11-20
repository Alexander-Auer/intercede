"""The ARC backend: what the shared suite cannot show on a healthy stack.

Normal behaviour is the shared integration suite's, on the arc-slurm stack.
These tests drive the backend through its public operations, against a
simulated A-REX, for what a stack does not produce: states it never reaches,
refusals, partial failures, and answers a CE gives when something is wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from intercede import (
    AuthenticationError,
    ContainerSpec,
    CopyVars,
    FileRef,
    JobHandle,
    JobID,
    JobStatus,
    OutputMember,
    OutputSpec,
    Resources,
    ResourceUnavailableError,
    SpecificationRejectedError,
)
from intercede._backends.monolith.arc import ARCBackend

from ._fake_arc import ENDPOINT, SPEC, FakeARC, ScopedTokens, make

HANDLE = JobHandle(backend="arc", resource=ENDPOINT, id=JobID("0123abcd"))


class TestStatus:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("ACCEPTING", JobStatus.WAITING),
            ("PREPARING", JobStatus.WAITING),
            ("QUEUING", JobStatus.WAITING),
            ("HELD", JobStatus.WAITING),  # the batch system may release it
            ("RUNNING", JobStatus.RUNNING),
            ("FINISHING", JobStatus.RUNNING),
            ("FINISHED", JobStatus.DONE),
            ("FAILED", JobStatus.FAILED),
            ("KILLING", JobStatus.ABORTED),
            ("KILLED", JobStatus.ABORTED),
            ("WIPED", JobStatus.UNKNOWN),  # its files are gone, and how it ended
            ("Running", JobStatus.RUNNING),  # whatever the case
            ("SOMETHING_NEW", JobStatus.UNKNOWN),
        ],
    )
    async def test_the_states_the_ce_reports(
        self, ce: ARCBackend, arc: FakeARC, state: str, expected: JobStatus
    ):
        handle = (await ce.submit(SPEC)).handles[0]
        (job,) = arc.created()
        job.state = state
        assert await ce.get_status([handle]) == {handle: expected}

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
        theirs = HANDLE.model_copy(update=foreign)
        assert await ce.get_status([theirs]) == {theirs: JobStatus.UNKNOWN}
        assert arc.requests == []


class TestSubmit:
    async def test_a_job_waits_for_its_own_secrets_and_never_describes_them(
        self, ce: ARCBackend, arc: FakeARC
    ):
        copies = [
            CopyVars(secrets={"pilot.secret": b"s3cret-%d" % i}) for i in range(2)
        ]
        await ce.submit(SPEC, copies)
        for i, job in enumerate(arc.created()):
            assert job.files["pilot.secret"] == b"s3cret-%d" % i
            # Listed without a source: ARC starts the job once it is uploaded.
            assert job.inputs()["pilot.secret"] is None
        (new,) = [request for request in arc.requests if request.action == "new"]
        assert b"s3cret" not in new.body

    @pytest.mark.parametrize(
        ("spec_queue", "config_queue", "expected"),
        [("short", "long", ["short"]), (None, "long", ["long"]), (None, None, [])],
        ids=["spec", "config", "the-ce-chooses"],
    )
    async def test_resources_and_the_queue_reach_the_ce(
        self,
        arc: FakeARC,
        spec_queue: str | None,
        config_queue: str | None,
        expected: list[str],
    ):
        resources = Resources(cpus=4, memory_mb=2000, queue=spec_queue)
        spec = SPEC.model_copy(update={"resources": resources})
        async with make(arc, queue=config_queue) as ce:
            await ce.submit(spec)
        (job,) = arc.created()
        assert job.texts("QueueName") == expected
        assert job.texts("IndividualPhysicalMemory") == [str(2000 * 1024 * 1024)]
        # All of a job's cores on one node, as on every other backend.
        assert job.texts("NumberOfSlots") == job.texts("SlotsPerHost") == ["4"]


def _input(tmp_path: Path, name: str) -> FileRef:
    (tmp_path / "source").write_text("data")
    return FileRef(name=name, source=str(tmp_path / "source"))


# Each is refused before the CE is contacted, or a token asked for.
REFUSED: dict[str, dict[str, Any]] = {
    "output-for-collection": {
        "outputs": OutputSpec(members=[OutputMember(pattern="result.txt")])
    },
    "unlisted-outputs": {"outputs": OutputSpec(include_unlisted=True)},
    "output-pattern-to-url": {
        "outputs": OutputSpec(
            members=[
                OutputMember(pattern="*.txt", destination="https://s.example.org/")
            ]
        )
    },
    "container-without-launcher": {"container": ContainerSpec(image="docker://x")},
    "argument-expanded-by-arc": {"arguments": ["$HOME"]},
    "argument-in-backticks": {"arguments": ["`id`"]},
    "argument-with-line-break": {"arguments": ["one\ntwo"]},
    "variable-with-line-break": {"environment": {"A": "one\ntwo"}},
    "variable-name": {"environment": {"1A": "x"}},
    "character-not-in-xml": {"arguments": ["\x00"]},
}


class TestRefused:
    @pytest.mark.parametrize("update", REFUSED.values(), ids=REFUSED.keys())
    async def test_before_the_ce_is_contacted(
        self, ce: ARCBackend, arc: FakeARC, tokens: ScopedTokens, update: dict[str, Any]
    ):
        with pytest.raises(SpecificationRejectedError):
            await ce.submit(SPEC.model_copy(update=update))
        assert arc.requests == []
        assert tokens.asked == []

    async def test_a_name_the_backend_uses_itself(
        self, ce: ARCBackend, arc: FakeARC, tmp_path: Path
    ):
        spec = SPEC.model_copy(
            update={"inputs": [_input(tmp_path, ".intercede-stdout")]}
        )
        with pytest.raises(SpecificationRejectedError, match="uses itself"):
            await ce.submit(spec)
        with pytest.raises(SpecificationRejectedError, match="uses itself"):
            await ce.submit(SPEC, [CopyVars(secrets={".intercede-executable": b"s"})])
        assert arc.requests == []


class TestPartlySubmitted:
    async def test_a_copy_the_ce_refuses_is_reported_and_the_others_submitted(
        self, ce: ARCBackend, arc: FakeARC
    ):
        arc.refused_copies = {1}
        submission = await ce.submit(SPEC, 3)
        assert sorted(submission.handles) == [0, 2]
        assert "Queue is not available" in submission.failures[1]

    async def test_every_copy_refused_fails_the_operation(
        self, ce: ARCBackend, arc: FakeARC
    ):
        arc.refused_copies = {0, 1}
        with pytest.raises(ResourceUnavailableError, match="Queue is not available"):
            await ce.submit(SPEC, 2)

    async def test_a_job_whose_files_did_not_all_arrive_is_cancelled(
        self, ce: ARCBackend, arc: FakeARC
    ):
        arc.failing_uploads = {"b.secret"}
        copies = [
            CopyVars(secrets={"a.secret": b"a"}),
            CopyVars(secrets={"b.secret": b"b"}),
        ]
        submission = await ce.submit(SPEC, copies)
        assert sorted(submission.handles) == [0]
        assert "the job was cancelled" in submission.failures[1]
        first, second = arc.created()
        assert (first.state, second.state) == ("ACCEPTING", "KILLING")

    async def test_when_no_job_got_its_files_the_operation_fails(
        self, ce: ARCBackend, arc: FakeARC, tmp_path: Path
    ):
        arc.failing_uploads = {"in.dat"}
        spec = SPEC.model_copy(update={"inputs": [_input(tmp_path, "in.dat")]})
        with pytest.raises(ResourceUnavailableError, match="Failed to write file"):
            await ce.submit(spec, 2)
        assert {job.state for job in arc.created()} == {"KILLING"}


class TestErrors:
    async def test_a_refused_token_is_never_quoted(self, ce: ARCBackend, arc: FakeARC):
        arc.refuse_token = True
        with pytest.raises(AuthenticationError, match="refused the token") as exc_info:
            await ce.submit(SPEC)
        assert "token:" not in str(exc_info.value)

    async def test_a_refused_connection_is_not_used_again(
        self, ce: ARCBackend, arc: FakeARC
    ):
        # A-REX refuses every later request on a connection it refused once.
        arc.refuse_token = True
        with pytest.raises(AuthenticationError):
            await ce.get_status([HANDLE])
        assert arc.closed == 1
        arc.refuse_token = False
        await ce.get_status([HANDLE])
        assert arc.actions() == ["status", "status"]

    @pytest.mark.parametrize("code", [401, 403])
    async def test_a_token_refused_with_another_status(
        self, ce: ARCBackend, arc: FakeARC, code: int
    ):
        arc.answer = httpx.Response(code)
        with pytest.raises(AuthenticationError):
            await ce.get_status([HANDLE])

    async def test_a_ce_that_fails_the_request(self, ce: ARCBackend, arc: FakeARC):
        arc.answer = httpx.Response(
            500, extensions={"reason_phrase": b"Internal Server Error"}
        )
        with pytest.raises(ResourceUnavailableError, match="500 Internal Server Error"):
            await ce.submit(SPEC)
        assert arc.closed == 0  # only a refused token drops the connection

    async def test_an_answer_that_is_not_arcs(self, ce: ARCBackend, arc: FakeARC):
        arc.answer = httpx.Response(201, text="<html>a proxy's error page</html>")
        with pytest.raises(ResourceUnavailableError, match="not ARC's"):
            await ce.get_status([HANDLE])
