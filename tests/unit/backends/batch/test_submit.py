"""What ``submit`` does that a Slurm stack does not show, on every transport.

What a job receives is the shared suite's, on the Slurm stacks. These are the
refusals, the failures, and what must stay private on the resource.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from intercede.exceptions import (
    ResourceUnavailableError,
    SpecificationRejectedError,
)
from intercede.models import (
    ContainerSpec,
    CopyVars,
    FileRef,
    JobHandle,
    OutputMember,
    OutputSpec,
    Resources,
    SubmissionSpec,
)

from ._fake_slurm import FakeSlurm

Script = Callable[..., str]


def single(submission: Any) -> JobHandle:
    return submission.handles[0]


def stream(home: Path, handle: JobHandle, name: str = "stdout") -> str:
    """Read a stream of a job from where the job's copy left it on the resource."""
    return (directory(home, handle) / name).read_text()


def directory(home: Path, handle: JobHandle) -> Path:
    return (
        home
        / ".intercede"
        / handle.routing["submission"]
        / "copies"
        / handle.routing["copy"]
    )


class TestSecretsAndResources:
    async def test_no_secret_is_in_what_the_scheduler_or_runner_can_read(
        self, backend: Any, home: Path, job_script: Script
    ):
        handle = single(
            await backend.submit(
                SubmissionSpec(executable=job_script()),
                [CopyVars(secrets={"key": b"top-secret"})],
            )
        )
        submission = directory(home, handle).parents[1]
        for name in ("run.sh", "job.sh"):
            assert "top-secret" not in (submission / name).read_text()

    async def test_only_the_account_can_read_the_submission(
        self, backend: Any, home: Path, job_script: Script
    ):
        handle = single(await backend.submit(SubmissionSpec(executable=job_script())))
        submission = directory(home, handle).parents[1]
        assert submission.stat().st_mode & 0o077 == 0

    async def test_resources_reach_the_scheduler(
        self, backend: Any, home: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script(),
            resources=Resources(cpus=2, memory_mb=100, queue="short"),
        )
        handle = single(await backend.submit(spec))
        script = (directory(home, handle).parents[1] / "job.sh").read_text()
        assert "--cpus-per-task=2" in script
        assert "--mem=100" in script
        assert "--partition=short" in script


class TestRefused:
    @pytest.mark.parametrize(
        ("fields", "match"),
        [
            (
                {"inputs": [FileRef(name="x", source="https://example.org/x")]},
                "cannot fetch inputs",
            ),
            (
                {
                    "outputs": OutputSpec(
                        members=[
                            OutputMember(
                                pattern="x", destination="https://example.org/x"
                            )
                        ]
                    )
                },
                "cannot upload outputs",
            ),
            (
                {"container": ContainerSpec(image="docker://almalinux:9")},
                "no container_launcher",
            ),
            ({"environment": {"NOT A NAME": "x"}}, "environment variable"),
            ({"environment": {"A=B; rm -rf /": "x"}}, "environment variable"),
            (
                {"inputs": [FileRef(name="x", source="/no/such/file")]},
                "cannot read /no/such/file",
            ),
        ],
        ids=["url-input", "url-output", "container", "env-space", "env-shell", "input"],
    )
    async def test_nothing_is_queued_or_left_behind(
        self,
        backend: Any,
        slurm: FakeSlurm,
        home: Path,
        job_script: Script,
        fields: dict[str, Any],
        match: str,
    ):
        spec = SubmissionSpec(executable=job_script(), **fields)
        with pytest.raises(SpecificationRejectedError, match=match):
            await backend.submit(spec)
        assert slurm.calls == []
        assert not (home / ".intercede").exists()

    async def test_a_copys_environment_is_checked_too(
        self, backend: Any, job_script: Script
    ):
        with pytest.raises(SpecificationRejectedError, match="environment variable"):
            await backend.submit(
                SubmissionSpec(executable=job_script()),
                [CopyVars(environment={"BAD NAME": "x"})],
            )

    async def test_a_queue_the_scheduler_cannot_express(
        self, backend: Any, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script(), resources=Resources(queue="a\n#SBATCH --x")
        )
        with pytest.raises(SpecificationRejectedError, match="partition"):
            await backend.submit(spec)


class TestFailure:
    async def test_a_refused_submission_leaves_nothing_behind(
        self, backend: Any, slurm: FakeSlurm, home: Path, job_script: Script
    ):
        slurm.break_tool("sbatch", "sbatch: error: Invalid partition name specified")
        with pytest.raises(ResourceUnavailableError, match="Invalid partition name"):
            await backend.submit(SubmissionSpec(executable=job_script()))
        assert list((home / ".intercede").iterdir()) == []

    async def test_unreadable_output_keeps_the_files_of_what_may_have_been_queued(
        self, backend: Any, slurm: FakeSlurm, home: Path, job_script: Script
    ):
        slurm.break_tool("sbatch", stdout="Submitted batch job 12", returncode=0)
        with pytest.raises(ResourceUnavailableError, match="unexpected sbatch output"):
            await backend.submit(SubmissionSpec(executable=job_script()))
        assert len(list((home / ".intercede").iterdir())) == 1


class TestContainer:
    async def test_the_launcher_starts_the_command(
        self, backend_with: Callable[..., Any], home: Path
    ):
        launcher = ["echo", "image={image}", "--"]
        async with backend_with(container_launcher=launcher) as backend:
            spec = SubmissionSpec(
                executable="program",
                arguments=["arg"],
                container=ContainerSpec(image="docker://almalinux:9"),
            )
            handle = single(await backend.submit(spec))
        assert stream(home, handle) == "image=docker://almalinux:9 -- program arg\n"

    async def test_a_local_file_cannot_run_inside_an_image(
        self, backend_with: Callable[..., Any], job_script: Script
    ):
        async with backend_with(container_launcher=["x", "{image}"]) as backend:
            spec = SubmissionSpec(
                executable=job_script(),
                container=ContainerSpec(image="docker://almalinux:9"),
            )
            with pytest.raises(SpecificationRejectedError, match="not a local file"):
                await backend.submit(spec)
