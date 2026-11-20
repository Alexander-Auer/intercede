"""The ARC backend: job descriptions, uploads and partly failed submissions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from intercede import (
    ContainerSpec,
    CopyVars,
    FileRef,
    OutputMember,
    OutputSpec,
    Resources,
    ResourceUnavailableError,
    SpecificationRejectedError,
)
from intercede._backends.monolith.arc import ARCBackend

from ._fake_arc import ENDPOINT, ON_THE_WORKER, SPEC, FakeARC, ScopedTokens, make


class TestSubmit:
    async def test_one_job_per_copy_in_one_request(self, ce: ARCBackend, arc: FakeARC):
        submission = await ce.submit(SPEC, 3)
        assert sorted(submission.handles) == [0, 1, 2]
        assert submission.failures == {}
        assert [handle.id for handle in submission.handles.values()] == list(arc.jobs)
        for handle in submission.handles.values():
            assert (handle.backend, handle.resource) == ("arc", ENDPOINT)
        assert arc.actions()[0] == "new" and arc.actions().count("new") == 1

    async def test_zero_copies_submit_nothing(self, ce: ARCBackend, arc: FakeARC):
        assert (await ce.submit(SPEC, 0)).handles == {}
        assert arc.requests == []

    async def test_each_copy_has_its_own_environment(
        self, ce: ARCBackend, arc: FakeARC
    ):
        spec = SPEC.model_copy(update={"environment": {"SITE": "x", "STAMP": "shared"}})
        await ce.submit(
            spec, [CopyVars(environment={"STAMP": str(i)}) for i in range(2)]
        )
        assert [job.environment() for job in arc.created()] == [
            {"SITE": "x", "STAMP": "0"},
            {"SITE": "x", "STAMP": "1"},
        ]

    async def test_secrets_are_uploaded_to_their_own_job_and_never_described(
        self, ce: ARCBackend, arc: FakeARC
    ):
        copies = [
            CopyVars(secrets={"pilot.secret": b"s3cret-%d" % i}) for i in range(2)
        ]
        await ce.submit(SPEC, copies)
        for i, job in enumerate(arc.created()):
            assert job.files["pilot.secret"] == b"s3cret-%d" % i
            assert job.inputs()["pilot.secret"] is None  # uploaded, not fetched
        (new,) = [request for request in arc.requests if request.action == "new"]
        assert b"s3cret" not in new.body

    async def test_a_local_executable_and_inputs_are_uploaded_to_every_job(
        self, ce: ARCBackend, arc: FakeARC, tmp_path: Path
    ):
        script = tmp_path / "job.sh"
        script.write_bytes(b"#!/bin/sh\ntrue\n")
        data = tmp_path / "anything.dat"
        data.write_bytes(b"data")
        spec = SPEC.model_copy(
            update={
                "executable": str(script),
                "arguments": ["--flag", 'it\'s "quoted"'],
                "inputs": [FileRef(name="sub/in.dat", source=str(data))],
            }
        )
        await ce.submit(spec, 2)
        for job in arc.created():
            (path,) = job.texts("Path")
            assert job.files == {path: b"#!/bin/sh\ntrue\n", "sub/in.dat": b"data"}
            assert job.texts("IsExecutable") == ["true"]
            assert job.texts("Argument") == ["--flag", 'it\'s "quoted"']

    async def test_a_command_on_the_worker_is_not_uploaded(
        self, ce: ARCBackend, arc: FakeARC
    ):
        await ce.submit(SPEC)
        (job,) = arc.created()
        assert job.texts("Path") == [ON_THE_WORKER]
        assert job.files == {}

    async def test_a_url_input_is_fetched_by_the_ce(self, ce: ARCBackend, arc: FakeARC):
        url = "https://data.example.org/in.dat"
        spec = SPEC.model_copy(update={"inputs": [FileRef(name="in.dat", source=url)]})
        await ce.submit(spec)
        (job,) = arc.created()
        assert job.inputs() == {"in.dat": url}
        assert job.files == {}

    async def test_a_url_output_is_uploaded_by_the_ce(
        self, ce: ARCBackend, arc: FakeARC
    ):
        url = "https://storage.example.org/result.txt"
        spec = SPEC.model_copy(
            update={
                "outputs": OutputSpec(
                    members=[OutputMember(pattern="result.txt", destination=url)]
                )
            }
        )
        await ce.submit(spec)
        (job,) = arc.created()
        assert url in job.texts("URI")

    async def test_streams_have_names_of_their_own_unless_given(
        self, ce: ARCBackend, arc: FakeARC
    ):
        await ce.submit(SPEC)
        await ce.submit(
            SPEC.model_copy(update={"stdout": "out.txt", "stderr": "out.txt"})
        )
        default, given = arc.created()
        assert default.texts("Output") != default.texts("Error")
        assert given.texts("Output") == given.texts("Error") == ["out.txt"]

    @pytest.mark.parametrize(
        ("spec_queue", "config_queue", "expected"),
        [("short", "long", ["short"]), (None, "long", ["long"]), (None, None, [])],
        ids=["spec", "config", "the-ce-chooses"],
    )
    async def test_the_queue(
        self,
        arc: FakeARC,
        spec_queue: str | None,
        config_queue: str | None,
        expected: list[str],
    ):
        spec = SPEC.model_copy(update={"resources": Resources(queue=spec_queue)})
        async with make(arc, queue=config_queue) as ce:
            await ce.submit(spec)
        (job,) = arc.created()
        assert job.texts("QueueName") == expected

    async def test_a_container_runs_through_the_launcher(self, arc: FakeARC):
        spec = SPEC.model_copy(
            update={
                "arguments": ["a"],
                "container": ContainerSpec(image="docker://almalinux:9"),
            }
        )
        async with make(arc, container_launcher=["apptainer", "exec", "{image}"]) as ce:
            await ce.submit(spec)
        (job,) = arc.created()
        assert job.texts("Path") == ["apptainer"]
        assert job.texts("Argument") == [
            "exec",
            "docker://almalinux:9",
            ON_THE_WORKER,
            "a",
        ]


def _input(tmp_path: Path, name: str) -> FileRef:
    (tmp_path / "source").write_text("data")
    return FileRef(name=name, source=str(tmp_path / "source"))


# Each is refused before the CE is contacted.
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

    async def test_a_local_input_that_cannot_be_read(
        self, ce: ARCBackend, arc: FakeARC, tmp_path: Path
    ):
        missing = FileRef(name="in.dat", source=str(tmp_path / "missing.dat"))
        with pytest.raises(SpecificationRejectedError, match="in.dat"):
            await ce.submit(SPEC.model_copy(update={"inputs": [missing]}))
        assert arc.requests == []

    async def test_a_secret_named_like_an_input(
        self, ce: ARCBackend, arc: FakeARC, tmp_path: Path
    ):
        spec = SPEC.model_copy(update={"inputs": [_input(tmp_path, "pilot.cfg")]})
        with pytest.raises(SpecificationRejectedError, match="pilot.cfg"):
            await ce.submit(spec, [CopyVars(secrets={"pilot.cfg": b"s"})])
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


class TestScopes:
    async def test_each_request_carries_only_its_operations_scopes(
        self, ce: ARCBackend, arc: FakeARC
    ):
        arc.failing_uploads = {"b.secret"}
        copies = [
            CopyVars(secrets={"a.secret": b"a"}),
            CopyVars(secrets={"b.secret": b"b"}),
        ]
        submission = await ce.submit(SPEC, copies)
        await ce.get_status(list(submission.handles.values()))
        create = frozenset({"compute.create", "compute.modify"})
        assert [(r.action or r.method, r.scopes) for r in arc.requests] == [
            ("new", create),
            ("PUT", create),
            ("PUT", create),
            ("kill", frozenset({"compute.cancel"})),
            ("status", frozenset({"compute.read"})),
        ]
