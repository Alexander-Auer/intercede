"""The Slurm core, against output copied from real Slurm 22.05 tools."""

from __future__ import annotations

import pytest

from intercede._backends._slurm import commands, status
from intercede._backends._slurm.submit import render
from intercede._backends.batch.protocols import CommandResult, Scheduler
from intercede._backends.batch.scheduler.slurm import Slurm
from intercede.exceptions import SpecificationRejectedError
from intercede.models import JobID, JobStatus, Resources, SubmissionSpec


def result(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def ids(*values: str) -> list[JobID]:
    return [JobID(value) for value in values]


class TestJobScript:
    def test_copies_are_one_array_and_start_the_runner(self):
        script = render(Resources(), 3, None)
        assert script.startswith("#!/bin/sh\n")
        assert "#SBATCH --array=0-2\n" in script
        assert "#SBATCH --output=copies/%a/log\n" in script
        assert script.endswith('exec sh run.sh "$SLURM_ARRAY_TASK_ID"\n')

    def test_the_environment_of_whoever_submits_stays_behind(self):
        assert "#SBATCH --export=NONE\n" in render(Resources(), 1, None)

    def test_resources_become_options(self):
        resources = Resources(cpus=4, memory_mb=2048, wall_time_s=90061, queue="long")
        script = render(resources, 1, None)
        assert "#SBATCH --cpus-per-task=4\n" in script
        assert "#SBATCH --mem=2048\n" in script
        assert "#SBATCH --time=25:01:01\n" in script
        assert "#SBATCH --partition=long\n" in script

    def test_what_is_not_asked_for_is_left_to_the_site(self):
        script = render(Resources(), 1, None)
        for option in ("--mem", "--time", "--partition"):
            assert option not in script

    def test_the_configured_queue_is_the_default(self):
        assert "--partition=short" in render(Resources(), 1, "short")
        assert "--partition=long" in render(Resources(queue="long"), 1, "short")

    @pytest.mark.parametrize("queue", ["a\n#SBATCH --uid=0", "-x", "a b", "a,b"])
    def test_a_queue_cannot_add_to_the_script(self, queue: str):
        with pytest.raises(SpecificationRejectedError, match="partition"):
            render(Resources(queue=queue), 1, None)


class TestSubmit:
    def test_the_command_runs_the_script_from_the_submission(self):
        command = Slurm().submit_command(
            SubmissionSpec(executable="true"), 2, ".intercede/abc"
        )
        assert command.argv == [
            "sbatch",
            "--parsable",
            "--chdir=.intercede/abc",
            ".intercede/abc/job.sh",
        ]
        assert "--array=0-1" in command.files["job.sh"]

    @pytest.mark.parametrize("printed", ["12\n", "12;intercede\n", "banner\n12\n"])
    def test_one_id_per_copy_from_the_array_id(self, printed: str):
        assert commands.parse_submit(result(printed), 3) == ids("12_0", "12_1", "12_2")

    def test_a_refused_job_says_why(self):
        refused = result(
            stderr="sbatch: error: invalid partition specified: nosuch\n"
            "sbatch: error: Batch job submission failed: Invalid partition name specified\n",
            returncode=1,
        )
        with pytest.raises(ValueError, match="Invalid partition name"):
            commands.parse_submit(refused, 1)

    def test_a_missing_tool_is_a_failure(self):
        missing = result(stderr="sh: sbatch: command not found\n", returncode=127)
        with pytest.raises(ValueError, match="command not found"):
            commands.parse_submit(missing, 1)

    @pytest.mark.parametrize("printed", ["", "Submitted batch job 12\n"])
    def test_unreadable_output_is_a_failure(self, printed: str):
        with pytest.raises(ValueError, match="unexpected sbatch output"):
            commands.parse_submit(result(printed), 1)


class TestJobIds:
    @pytest.mark.parametrize("value", ["12_0", "1234567_999"])
    def test_a_task_of_an_array_is_one(self, value: str):
        assert commands.is_job_id(value)

    @pytest.mark.parametrize(
        "value", ["", "12", "12.0", "-j", "12_0,13_0", "12_0 ", "12_[0-2]", "١٢_٠x"]
    )
    def test_anything_else_is_not(self, value: str):
        assert not commands.is_job_id(value)


class TestQueue:
    def test_the_command(self):
        assert commands.query(ids("1_0", "1_1")) == [
            "squeue",
            "-h",
            "-r",
            "-t",
            "all",
            "-j",
            "1_0,1_1",
            "-o",
            "%i|%T",
        ]

    def test_tasks_it_knows(self):
        printed = "1_0|COMPLETED\n1_1|FAILED\n2_0|PENDING\n3_0|RUNNING\n"
        assert status.parse_queue(result(printed)) == {
            "1_0": JobStatus.DONE,
            "1_1": JobStatus.FAILED,
            "2_0": JobStatus.WAITING,
            "3_0": JobStatus.RUNNING,
        }

    def test_not_knowing_any_job_is_an_answer(self):
        forgotten = result(
            stderr="slurm_load_jobs error: Invalid job id specified\n", returncode=1
        )
        assert status.parse_queue(forgotten) == {}

    def test_a_controller_that_is_down_is_a_failure(self):
        down = result(
            stderr="slurm_load_jobs error: Unable to contact slurm controller\n",
            returncode=1,
        )
        with pytest.raises(ValueError, match="Unable to contact"):
            status.parse_queue(down)

    def test_lines_that_are_not_rows_are_ignored(self):
        assert status.parse_queue(result("Welcome to the cluster\n1_0|RUNNING\n")) == {
            "1_0": JobStatus.RUNNING
        }


class TestAccounting:
    def test_the_command_asks_about_whole_arrays(self):
        assert commands.accounting(ids("1_0", "1_1", "2_0")) == [
            "sacct",
            "-n",
            "-X",
            "-P",
            "-j",
            "1,2",
            "-o",
            "JobID,State",
        ]

    def test_finished_tasks(self):
        printed = "1_0|COMPLETED\n1_1|FAILED\n1_2|CANCELLED by 1000\n"
        assert status.parse_accounting(result(printed), ids("1_0", "1_1", "1_2")) == {
            "1_0": JobStatus.DONE,
            "1_1": JobStatus.FAILED,
            "1_2": JobStatus.ABORTED,
        }

    def test_tasks_still_waiting_are_written_as_a_range(self):
        printed = "1_[0-1,3%2]|PENDING\n"
        assert status.parse_accounting(result(printed), ids("1_1", "1_3")) == {
            "1_1": JobStatus.WAITING,
            "1_3": JobStatus.WAITING,
        }

    def test_a_task_cancelled_while_it_waited_leaves_only_its_array(self):
        # What is left of the array, or the array itself when no task is left.
        assert status.parse_accounting(result("8_1|COMPLETED\n"), ids("8_0")) == {
            "8_0": JobStatus.ABORTED
        }
        assert status.parse_accounting(result("7|CANCELLED by 1000\n"), ids("7_0")) == {
            "7_0": JobStatus.ABORTED
        }

    def test_an_array_it_does_not_know_is_left_out(self):
        assert status.parse_accounting(result(""), ids("999_0")) == {}

    def test_a_site_without_accounting_knows_nothing(self):
        disabled = result(stderr="Slurm accounting storage is disabled\n", returncode=1)
        assert status.parse_accounting(disabled, ids("1_0")) == {}

    def test_an_accounting_that_is_down_is_a_failure(self):
        down = result(
            stderr="sacct: error: slurmdbd: Connection refused\n", returncode=1
        )
        with pytest.raises(ValueError, match="Connection refused"):
            status.parse_accounting(down, ids("1_0"))


class TestStates:
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
    def test_the_map(self, state: str, expected: JobStatus):
        assert status.job_status(state) == expected


class TestScheduler:
    def test_it_satisfies_the_protocol(self):
        assert isinstance(Slurm(), Scheduler)

    def test_it_moves_no_files_itself(self):
        assert Slurm().stages_own_files is False

    def test_kill_cancels_each_job(self):
        assert Slurm().kill_command(ids("1_0", "2_1")) == ["scancel", "1_0", "2_1"]
