"""Slurm as a composed backend's scheduler; the knowledge of Slurm itself is in ``_slurm``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Self

from intercede._backends._slurm import commands, status
from intercede._backends._slurm.submit import render
from intercede._backends.batch.protocols import (
    Command,
    CommandResult,
    Scheduler,
    SchedulerStatusMap,
    SubmitCommand,
)
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.models import JobID, SubmissionSpec

_SCRIPT = "job.sh"


class Slurm:
    """Queues copies as the tasks of one job array."""

    stages_own_files = False

    def __init__(self, queue: str | None = None) -> None:
        self._queue = queue

    @classmethod
    def from_settings(cls, settings: BatchLocalConfig | BatchSSHConfig) -> Self:
        """Build the scheduler; how the registry constructs it.

        Args:
            settings: The batch backend's validated configuration, whose
                ``queue`` is the partition of a job that names none.

        Returns:
            A new scheduler.
        """
        return cls(settings.queue)

    def is_job_id(self, value: str) -> bool:
        """Tell whether a stored ID is a task of an array: ``<array>_<task>``."""
        return commands.is_job_id(value)

    def submit_command(
        self, spec: SubmissionSpec, count: int, workdir: str
    ) -> SubmitCommand:
        """Build the ``sbatch`` of one array, with its job script."""
        return SubmitCommand(
            argv=commands.submit(f"{workdir}/{_SCRIPT}", workdir),
            files={_SCRIPT: render(spec.resources, count, self._queue)},
        )

    def parse_submit(self, result: CommandResult, count: int) -> Sequence[JobID]:
        """Read the array's ID and return the ID of each of its tasks."""
        return commands.parse_submit(result, count)

    def status_command(self, ids: Sequence[JobID]) -> Command:
        """Build the ``squeue`` that asks the controller."""
        return commands.query(ids)

    def parse_status(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        """Read the tasks ``squeue`` reports."""
        return status.parse_queue(result)

    def history_command(self, ids: Sequence[JobID]) -> Command:
        """Build the ``sacct`` that asks the accounting."""
        return commands.accounting(ids)

    def parse_history(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        """Read the tasks ``sacct`` can tell about."""
        return status.parse_accounting(result, ids)

    def kill_command(self, ids: Sequence[JobID]) -> Command:
        """Build the ``scancel``."""
        return commands.cancel(ids)


# The type checker proves conformance.
_scheduler: type[Scheduler] = Slurm
