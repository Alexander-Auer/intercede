"""What ``get_output`` does that a Slurm stack does not show, on every transport.

Collecting output is the shared suite's, on the Slurm stacks. These are what must
never come back, the limits, and collections that fail or race.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from intercede.models import (
    CopyVars,
    FileRef,
    OutputMember,
    OutputSpec,
    SubmissionSpec,
)

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


class TestWhatStaysBehind:
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


class TestPurged:
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
