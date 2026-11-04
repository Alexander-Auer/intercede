"""What ``kill`` and ``purge`` do, on every transport."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from intercede.exceptions import ResourceUnavailableError
from intercede.models import JobHandle, JobID, JobStatus, SubmissionSpec

from ._fake_slurm import FakeSlurm

Script = Callable[..., str]


async def submitted(backend: Any, script: str, copies: int = 1) -> list[JobHandle]:
    submission = await backend.submit(SubmissionSpec(executable=script), copies=copies)
    return list(submission.handles.values())


def submission_dir(home: Path, handle: JobHandle) -> Path:
    return home / ".intercede" / handle.routing["submission"]


class TestKill:
    async def test_a_running_job_is_cancelled(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        slurm.hold()
        handle = (await submitted(backend, job_script()))[0]
        slurm.run(handle.id)

        outcome = (await backend.kill([handle]))[handle]

        assert outcome.ok and outcome.reason is None
        assert (await backend.get_status([handle]))[handle] == JobStatus.ABORTED

    async def test_a_waiting_job_ends_up_aborted_too(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        slurm.hold()
        first, second = await submitted(backend, job_script(), copies=2)

        assert (await backend.kill([first, second]))[first].ok

        # Slurm drops it from its array: not in the queue, no row of its own.
        statuses = await backend.get_status([first, second])
        assert set(statuses.values()) == {JobStatus.ABORTED}

    async def test_a_finished_job_is_left_alone(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        handle = (await submitted(backend, job_script()))[0]

        outcome = (await backend.kill([handle]))[handle]

        assert outcome.ok and outcome.reason == "already finished"
        assert not [call for call in slurm.calls if call[0] == "scancel"]
        assert (await backend.get_status([handle]))[handle] == JobStatus.DONE

    async def test_each_job_gets_its_own_outcome(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        done = (await submitted(backend, job_script()))[0]
        slurm.hold()
        waiting = (await submitted(backend, job_script()))[0]
        unknown = done.model_copy(update={"id": JobID("999_0")})
        theirs = done.model_copy(update={"resource": "elsewhere"})

        outcomes = await backend.kill([done, waiting, unknown, theirs])

        assert outcomes[done].ok and outcomes[done].reason == "already finished"
        assert outcomes[waiting].ok and outcomes[waiting].reason is None
        assert not outcomes[unknown].ok and outcomes[unknown].reason == "unknown job"
        assert not outcomes[theirs].ok and outcomes[theirs].reason == "unknown job"

    async def test_a_job_that_stays_active_reports_why(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        slurm.hold()
        handle = (await submitted(backend, job_script()))[0]
        slurm.break_tool("scancel", "scancel: error: Access/permission denied")

        outcome = (await backend.kill([handle]))[handle]

        assert not outcome.ok
        assert outcome.reason == "scancel: error: Access/permission denied"

    async def test_a_job_that_ended_while_the_cancel_failed_is_fine(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        slurm.hold()
        handle = (await submitted(backend, job_script()))[0]
        slurm.break_tool("scancel", "scancel: error: Job/step already completing")
        slurm.finish_during_cancel(handle.id)

        assert (await backend.kill([handle]))[handle].ok

    async def test_a_scheduler_that_is_down_fails_the_whole_call(
        self, backend: Any, slurm: FakeSlurm, job_script: Script
    ):
        handle = (await submitted(backend, job_script()))[0]
        slurm.break_tool("squeue", "squeue: error: Unable to contact slurm controller")
        with pytest.raises(ResourceUnavailableError):
            await backend.kill([handle])


class TestPurge:
    async def test_a_finished_jobs_files_go_and_it_keeps_its_result(
        self, backend: Any, home: Path, job_script: Script
    ):
        handle = (await submitted(backend, job_script()))[0]
        assert submission_dir(home, handle).exists()

        assert (await backend.purge([handle]))[handle].ok

        assert not submission_dir(home, handle).exists()
        assert (await backend.get_status([handle]))[handle] == JobStatus.DONE

    async def test_an_active_job_is_cancelled_first(
        self, backend: Any, slurm: FakeSlurm, home: Path, job_script: Script
    ):
        slurm.hold()
        handle = (await submitted(backend, job_script()))[0]
        slurm.run(handle.id)

        assert (await backend.purge([handle]))[handle].ok

        assert ["scancel", handle.id] in slurm.calls
        assert not submission_dir(home, handle).exists()

    async def test_a_job_that_stays_active_keeps_its_files(
        self, backend: Any, slurm: FakeSlurm, home: Path, job_script: Script
    ):
        slurm.hold()
        handle = (await submitted(backend, job_script()))[0]
        slurm.break_tool("scancel", "scancel: error: Access/permission denied")

        outcome = (await backend.purge([handle]))[handle]

        assert not outcome.ok and "permission denied" in (outcome.reason or "")
        assert submission_dir(home, handle).exists()

    async def test_the_other_copies_keep_what_they_share(
        self, backend: Any, home: Path, job_script: Script
    ):
        first, second = await submitted(backend, job_script(), copies=2)

        await backend.purge([first])

        directory = submission_dir(home, first)
        assert not (directory / "copies/0").exists()
        assert (directory / "copies/1").exists()
        assert (directory / "run.sh").exists()

        await backend.purge([second])
        assert not directory.exists()

    async def test_purging_twice_is_harmless(self, backend: Any, job_script: Script):
        handle = (await submitted(backend, job_script()))[0]
        await backend.purge([handle])
        assert (await backend.purge([handle]))[handle].ok

    async def test_a_handle_that_is_not_ours_touches_nothing(
        self, backend: Any, home: Path, job_script: Script
    ):
        handle = (await submitted(backend, job_script()))[0]
        victim = home / "precious"
        victim.mkdir()
        forged = handle.model_copy(
            update={"routing": {"submission": "../precious", "copy": "0"}}
        )
        theirs = handle.model_copy(update={"resource": "elsewhere"})

        outcomes = await backend.purge([forged, theirs])

        assert not outcomes[forged].ok and not outcomes[theirs].ok
        assert victim.exists()
        assert submission_dir(home, handle).exists()
