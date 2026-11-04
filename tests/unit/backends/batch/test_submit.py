"""What ``submit`` does, on every transport."""

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
    JobStatus,
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


class TestHandles:
    async def test_one_handle_per_copy_from_one_array(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        submission = await backend.submit(
            SubmissionSpec(executable=job_script()), copies=3
        )
        assert submission.failures == {}
        assert [submission.handles[i].id for i in range(3)] == ["1_0", "1_1", "1_2"]
        assert [call[0] for call in slurm.calls] == ["sbatch"]  # not one per copy

    async def test_a_handle_names_the_backend_and_resource(
        self, backend: Any, job_script: Script
    ):
        handle = single(await backend.submit(SubmissionSpec(executable=job_script())))
        assert handle.backend == "batch"
        assert handle.resource == backend.resource

    async def test_a_handle_survives_being_stored(
        self, backend: Any, job_script: Script
    ):
        handle = single(await backend.submit(SubmissionSpec(executable=job_script())))
        stored = JobHandle.model_validate_json(handle.model_dump_json())
        assert (await backend.get_status([stored]))[stored] == JobStatus.DONE

    async def test_no_copies_submit_nothing(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        submission = await backend.submit(SubmissionSpec(executable=job_script()), [])
        assert submission.handles == {}
        assert slurm.calls == []


class TestWhatAJobGets:
    async def test_the_program_and_its_arguments_arrive_exactly(
        self, backend: Any, home: Path, job_script: Script
    ):
        awkward = ["two words", "it's", "$HOME", "*", "", "a\nb"]
        spec = SubmissionSpec(
            executable=job_script('for a in "$@"; do echo "[$a]"; done'),
            arguments=awkward,
        )
        handle = single(await backend.submit(spec))
        assert stream(home, handle) == "".join(f"[{arg}]\n" for arg in awkward)

    async def test_a_command_on_the_resource_is_not_shipped(
        self, backend: Any, home: Path
    ):
        spec = SubmissionSpec(executable="echo", arguments=["from", "the worker"])
        handle = single(await backend.submit(spec))
        assert stream(home, handle) == "from the worker\n"
        assert not (directory(home, handle).parents[1] / "executable").exists()

    async def test_a_script_runs_without_its_execute_bit(
        self, backend: Any, home: Path, tmp_path: Path
    ):
        script = tmp_path / "plain.sh"
        script.write_text("#!/bin/sh\necho ran\n")
        script.chmod(0o644)
        handle = single(await backend.submit(SubmissionSpec(executable=str(script))))
        assert stream(home, handle) == "ran\n"

    async def test_each_copy_has_its_own_environment_over_the_shared_one(
        self, backend: Any, home: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script('echo "$SHARED $WHO ${ONLY_FIRST-unset}"'),
            environment={"SHARED": "both", "WHO": "nobody"},
        )
        copies = [
            CopyVars(environment={"WHO": "it's me", "ONLY_FIRST": "yes"}),
            CopyVars(environment={"WHO": "$HOME"}),
            CopyVars(),
        ]
        submission = await backend.submit(spec, copies)
        assert [stream(home, h) for h in submission.handles.values()] == [
            "both it's me yes\n",
            "both $HOME unset\n",
            "both nobody unset\n",
        ]

    async def test_each_copy_starts_in_its_own_directory_with_the_inputs(
        self, backend: Any, home: Path, tmp_path: Path, job_script: Script
    ):
        (tmp_path / "data.txt").write_text("shared")
        spec = SubmissionSpec(
            executable=job_script("cat sub/data.txt; echo; echo mine >> sub/data.txt"),
            inputs=[FileRef(name="sub/data.txt", source=str(tmp_path / "data.txt"))],
        )
        submission = await backend.submit(spec, copies=2)
        for handle in submission.handles.values():
            assert stream(home, handle) == "shared\n"  # not what the other wrote

    async def test_a_secret_is_only_its_copys_and_private(
        self, backend: Any, home: Path, job_script: Script
    ):
        spec = SubmissionSpec(executable=job_script("cat key; stat -c %a key"))
        copies = [CopyVars(secrets={"key": b"a\n"}), CopyVars(secrets={"key": b"b\n"})]
        submission = await backend.submit(spec, copies)
        assert [stream(home, h) for h in submission.handles.values()] == [
            "a\n600\n",
            "b\n600\n",
        ]

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

    async def test_named_streams_land_in_the_working_directory(
        self, backend: Any, home: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script("echo out; echo err >&2"),
            stdout="logs/out.txt",
            stderr="logs/err.txt",
        )
        handle = single(await backend.submit(spec))
        work = directory(home, handle) / "work"
        assert (work / "logs/out.txt").read_text() == "out\n"
        assert (work / "logs/err.txt").read_text() == "err\n"

    async def test_one_named_file_can_hold_both_streams(
        self, backend: Any, home: Path, job_script: Script
    ):
        spec = SubmissionSpec(
            executable=job_script("echo out; echo err >&2"),
            stdout="all.txt",
            stderr="all.txt",
        )
        handle = single(await backend.submit(spec))
        assert (directory(home, handle) / "work/all.txt").read_text() == "out\nerr\n"

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

    async def test_a_secret_named_like_an_input(
        self, backend: Any, tmp_path: Path, job_script: Script
    ):
        (tmp_path / "x").write_text("input")
        spec = SubmissionSpec(
            executable=job_script(),
            inputs=[FileRef(name="x", source=str(tmp_path / "x"))],
        )
        with pytest.raises(SpecificationRejectedError, match="named like inputs"):
            await backend.submit(spec, [CopyVars(secrets={"x": b"secret"})])

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
