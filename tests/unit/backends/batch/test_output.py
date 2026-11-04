"""What ``get_output`` does, on every transport."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from intercede._backends.batch.collect import Entry, parse_listing, select
from intercede._backends.batch.job import Manifest
from intercede.models import (
    CopyVars,
    FileRef,
    JobID,
    OutputMember,
    OutputSpec,
    SubmissionSpec,
)

from ._fake_slurm import FakeSlurm

Script = Callable[..., str]


async def collected(
    backend: Any, spec: SubmissionSpec, dest: Path, **kwargs: Any
) -> Any:
    handle = (await backend.submit(spec, **kwargs)).handles[0]
    return handle, (await backend.get_output([handle], dest))[handle]


def wanted(*members: str, include_unlisted: bool = False) -> OutputSpec:
    return OutputSpec(
        members=[OutputMember(pattern=member) for member in members],
        include_unlisted=include_unlisted,
    )


class TestWhatComesBack:
    async def test_streams_and_listed_files(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script(
                "echo out; echo err >&2; echo 1 > a.txt; echo 2 > b.txt"
            ),
            outputs=wanted("a.txt"),
        )
        _, outcome = await collected(backend, spec, tmp_path / "out")

        assert outcome.ok, outcome.reason
        output = outcome.value
        assert output.stdout.read_text() == "out\n"
        assert output.stderr.read_text() == "err\n"
        assert output.log is not None
        assert set(output.files) == {"a.txt"}
        assert output.files["a.txt"].read_text() == "1\n"

    async def test_no_output_spec_brings_only_the_streams(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(executable=job_script("echo hi; echo 1 > file"))
        _, outcome = await collected(backend, spec, tmp_path / "out")
        assert outcome.value.files == {}
        assert outcome.value.stdout.read_text() == "hi\n"

    async def test_globs_and_directories_are_members(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script(
                "mkdir -p d/e; echo 1 > d/e/f; echo 2 > a.log; echo 3 > b.log; echo 4 > c.txt"
            ),
            outputs=wanted("d", "*.log"),
        )
        _, outcome = await collected(backend, spec, tmp_path / "out")
        assert set(outcome.value.files) == {"d/e/f", "a.log", "b.log"}

    async def test_a_payload_may_use_the_names_of_the_streams(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script(
                "echo to-stdout; echo file-stdout > stdout; echo file-log > log"
            ),
            outputs=wanted(include_unlisted=True),
        )
        _, outcome = await collected(backend, spec, tmp_path / "out")
        output = outcome.value
        assert output.stdout.read_text() == "to-stdout\n"
        assert output.files["stdout"].read_text() == "file-stdout\n"
        assert output.files["log"].read_text() == "file-log\n"

    async def test_include_unlisted_leaves_out_inputs_and_secrets(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        (tmp_path / "in.txt").write_text("input")
        spec = SubmissionSpec(
            executable=job_script("echo 1 > made.txt"),
            inputs=[FileRef(name="in.txt", source=str(tmp_path / "in.txt"))],
            outputs=wanted(include_unlisted=True),
        )
        _, outcome = await collected(
            backend,
            spec,
            tmp_path / "out",
            copies=[CopyVars(secrets={"key": b"secret"})],
        )
        assert set(outcome.value.files) == {"made.txt"}

    async def test_a_glob_does_not_bring_back_a_secret_but_a_name_brings_an_input(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        (tmp_path / "in.txt").write_text("input")
        spec = SubmissionSpec(
            executable=job_script("echo changed > in.txt; echo 1 > made.txt"),
            inputs=[FileRef(name="in.txt", source=str(tmp_path / "in.txt"))],
            outputs=wanted("in.txt", "*"),
        )
        _, outcome = await collected(
            backend,
            spec,
            tmp_path / "out",
            copies=[CopyVars(secrets={"key": b"secret"})],
        )
        assert set(outcome.value.files) == {"in.txt", "made.txt"}
        assert outcome.value.files["in.txt"].read_text() == "changed\n"

    async def test_named_streams_come_back_as_streams(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script("echo out; echo err >&2"),
            stdout="logs/o.txt",
            stderr="logs/e.txt",
            outputs=wanted(include_unlisted=True),
        )
        _, outcome = await collected(backend, spec, tmp_path / "out")
        assert outcome.value.stdout.read_text() == "out\n"
        assert outcome.value.stderr.read_text() == "err\n"
        assert outcome.value.files == {}

    async def test_awkward_names_arrive(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script("printf x > 'a b'; printf x > \"$(printf 'c\\td')\""),
            outputs=wanted(include_unlisted=True),
        )
        _, outcome = await collected(backend, spec, tmp_path / "out")
        assert set(outcome.value.files) == {"a b", "c\td"}

    async def test_a_failed_job_can_be_collected(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(executable=job_script("echo why; exit 3"))
        _, outcome = await collected(backend, spec, tmp_path / "out")
        assert outcome.value.stdout.read_text() == "why\n"

    async def test_everything_lands_under_dest(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        dest = tmp_path / "out"
        spec = SubmissionSpec(executable=job_script("echo 1 > a"), outputs=wanted("a"))
        _, outcome = await collected(backend, spec, dest)
        output = outcome.value
        for path in (output.stdout, output.stderr, output.log, *output.files.values()):
            assert path.resolve().is_relative_to(dest.resolve())


class TestAgain:
    async def test_output_can_be_collected_again(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(executable=job_script("echo once"))
        handle, first = await collected(backend, spec, tmp_path / "one")
        again = (await backend.get_output([handle], tmp_path / "two"))[handle]
        assert first.value.stdout.read_text() == "once\n"
        assert again.value.stdout.read_text() == "once\n"

    async def test_collecting_into_the_same_place_replaces_it(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(executable=job_script("echo once"))
        handle, first = await collected(backend, spec, tmp_path / "out")
        first.value.stdout.write_text("changed by the consumer")
        (first.value.stdout.parent / "stray").write_text("left over")

        again = (await backend.get_output([handle], tmp_path / "out"))[handle]

        assert again.value.stdout.read_text() == "once\n"
        assert not (again.value.stdout.parent / "stray").exists()
        assert [p.name for p in again.value.stdout.parents[1].iterdir()] == [handle.id]

    async def test_a_purged_job_has_no_output(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(executable=job_script())
        handle, _ = await collected(backend, spec, tmp_path / "out")
        await backend.purge([handle])
        outcome = (await backend.get_output([handle], tmp_path / "again"))[handle]
        assert not outcome.ok and "purged" in (outcome.reason or "")

    async def test_a_purged_copy_has_no_output_while_another_keeps_its_own(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        submission = await backend.submit(
            SubmissionSpec(executable=job_script()), copies=2
        )
        first, second = submission.handles[0], submission.handles[1]
        await backend.purge([first])
        outcomes = await backend.get_output([first, second], tmp_path / "out")
        assert not outcomes[first].ok and "purged" in (outcomes[first].reason or "")
        assert outcomes[second].ok


class TestRefused:
    async def test_each_job_gets_its_own_outcome(
        self, backend: Any, slurm: FakeSlurm, tmp_path: Path, job_script: Script
    ):
        done = (await backend.submit(SubmissionSpec(executable=job_script()))).handles[
            0
        ]
        slurm.hold()
        waiting = (
            await backend.submit(SubmissionSpec(executable=job_script()))
        ).handles[0]
        unknown = done.model_copy(update={"id": JobID("999_0")})
        theirs = done.model_copy(update={"resource": "elsewhere"})

        outcomes = await backend.get_output(
            [done, waiting, unknown, theirs], tmp_path / "out"
        )

        assert list(outcomes) == [done, waiting, unknown, theirs]
        assert outcomes[done].ok
        assert outcomes[waiting].reason == "job has not finished"
        assert outcomes[unknown].reason == "unknown job"
        assert outcomes[theirs].reason == "unknown job"

    async def test_the_same_handle_twice_is_collected_once(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        handle = (
            await backend.submit(SubmissionSpec(executable=job_script()))
        ).handles[0]
        assert (await backend.get_output([handle, handle], tmp_path / "out"))[handle].ok

    async def test_a_link_is_never_followed(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script("ln -s /etc/passwd stolen"),
            outputs=wanted(include_unlisted=True),
        )
        _, outcome = await collected(backend, spec, tmp_path / "out")
        assert not outcome.ok and "link" in (outcome.reason or "")

    @pytest.mark.parametrize(
        ("limit", "match"),
        [
            ({"max_output_files": 2}, "more than 2 output files"),
            ({"max_output_bytes": 10}, "larger than 10 bytes"),
        ],
        ids=["files", "bytes"],
    )
    async def test_limits_refuse_before_anything_is_copied(
        self,
        backend_with: Callable[..., Any],
        tmp_path: Path,
        job_script: Script,
        limit: dict[str, int],
        match: str,
    ):
        async with backend_with(**limit) as backend:
            spec = SubmissionSpec(
                executable=job_script("echo 0123456789 > a; echo 1 > b; echo 2 > c"),
                outputs=wanted(include_unlisted=True),
            )
            _, outcome = await collected(backend, spec, tmp_path / "out")
        assert not outcome.ok and match in (outcome.reason or "")
        assert not [p for p in (tmp_path / "out").rglob("*") if p.is_file()]

    @pytest.mark.skipif(os.getuid() == 0, reason="root reads every file")
    async def test_a_copy_that_fails_keeps_the_previous_collection(
        self, backend: Any, home: Path, tmp_path: Path, job_script: Script
    ):
        spec = SubmissionSpec(executable=job_script("echo once"))
        handle, first = await collected(backend, spec, tmp_path / "out")
        copy = home / ".intercede" / handle.routing["submission"] / "copies/0"
        (copy / "stdout").chmod(0)

        outcome = (await backend.get_output([handle], tmp_path / "out"))[handle]

        assert not outcome.ok and outcome.reason
        assert first.value.stdout.read_text() == "once\n"
        assert [p.name for p in first.value.stdout.parents[1].iterdir()] == [handle.id]


class TestListing:
    def test_a_type_a_size_and_a_name(self):
        raw = "f\t12\twork/a b\0l\t4\twork/link\0f\t0\tname\twith\ttabs\0"
        assert parse_listing(raw) == [
            Entry("work/a b", 12, is_link=False),
            Entry("work/link", 4, is_link=True),
            Entry("name\twith\ttabs", 0, is_link=False),
        ]

    def test_nothing_is_nothing(self):
        assert parse_listing("") == []

    @pytest.mark.parametrize("raw", ["garbage\0", "f\tmany\tname\0"])
    def test_what_cannot_be_read_is_refused(self, raw: str):
        with pytest.raises(ValueError):
            parse_listing(raw)

    def test_a_name_that_leaves_the_directory_is_refused(self):
        manifest = Manifest(stdout="stdout", stderr="stderr", include_unlisted=True)
        with pytest.raises(ValueError, match="outside"):
            select(
                [Entry("work/../../../etc/passwd", 1, is_link=False)],
                manifest,
                max_files=10,
                max_bytes=10,
            )
