"""HTCondor as a composed backend's scheduler; what HTCondor itself knows is in ``_htcondor``.

It queues every copy of a submission as one cluster (``queue N``), on the schedd of
the host the transport reaches, and asks that schedd about the jobs. A job moves its
own files: HTCondor copies what every copy shares and the job's own copy's directory
to the node that runs it, and brings that directory back when the job ends
(``_htcondor.submit.TransferPlan``). So the nodes need no file system they share
with that host, and a copy never gets another copy's secrets.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Self

from intercede._backends._htcondor import commands, status
from intercede._backends._htcondor.submit import PROC_ID, TransferPlan, render_transfer
from intercede._backends.batch import layout
from intercede._backends.batch.protocols import (
    Command,
    CommandResult,
    Scheduler,
    SchedulerStatusMap,
    SubmitCommand,
)
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.exceptions import ConfigurationError, SpecificationRejectedError
from intercede.models import JobID, SubmissionSpec

_DESCRIPTION = "job.sub"
# The runner is a shell script: every node of a pool has /bin/sh.
_SHELL = "/bin/sh"


class HTCondor:
    """Queues copies as the processes of one cluster, each moving its own files."""

    stages_own_files = True

    @classmethod
    def from_settings(cls, settings: BatchLocalConfig | BatchSSHConfig) -> Self:
        """Build the scheduler; how the registry constructs it.

        Args:
            settings: The batch backend's validated configuration.

        Returns:
            A new scheduler.

        Raises:
            ConfigurationError: ``queue`` is set: a schedd has no queues.
        """
        if settings.queue is not None:
            raise ConfigurationError(
                f"queue {settings.queue!r}: an HTCondor schedd has no queues"
            )
        return cls()

    def is_job_id(self, value: str) -> bool:
        """Tell whether a stored ID is a job's: ``<cluster>.<proc>``."""
        return commands.is_job_id(value)

    def submit_command(
        self, spec: SubmissionSpec, count: int, workdir: str
    ) -> SubmitCommand:
        """Build the ``condor_submit`` of one cluster, with its submit description.

        Raises:
            SpecificationRejectedError: ``spec`` asks for a queue, which a schedd
                does not have.
        """
        if spec.resources.queue is not None:
            raise SpecificationRejectedError(
                f"queue {spec.resources.queue!r}: an HTCondor schedd has no queues"
            )
        own = layout.copy_dir(PROC_ID)
        plan = TransferPlan(
            executable=_SHELL,
            arguments=[layout.RUNNER, PROC_ID],  # sh run.sh <copy>
            initialdir=workdir,
            inputs=[*layout.shared(spec), own],
            outputs=[own],
            log=layout.scheduler_log(PROC_ID),
            count=count,
            cpus=spec.resources.cpus,
            memory_mb=spec.resources.memory_mb,
            wall_time_s=spec.resources.wall_time_s,
        )
        try:
            description = render_transfer(plan)
        except ValueError as exc:  # a value a description cannot hold
            raise SpecificationRejectedError(str(exc)) from exc
        return SubmitCommand(
            argv=commands.submit(f"{workdir}/{_DESCRIPTION}", spool=False),
            files={_DESCRIPTION: description},
        )

    def parse_submit(self, result: CommandResult, count: int) -> Sequence[JobID]:
        """Read the cluster's processes from ``condor_submit -terse``."""
        if result.returncode != 0:
            raise ValueError(_reason(result, "condor_submit"))
        ids = commands.parse_submit(result.stdout)
        if len(ids) != count:
            raise ValueError(f"condor_submit queued {len(ids)} jobs, not {count}")
        return ids

    def status_command(self, ids: Sequence[JobID]) -> Command:
        """Build the ``condor_q`` that asks the schedd's queue."""
        return commands.query(ids, status.STATE_ATTRIBUTES, ())

    def parse_status(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        """Read the jobs ``condor_q`` reports; it prints nothing for one it does not know."""
        return _states(result, "condor_q")

    def history_command(self, ids: Sequence[JobID]) -> Command:
        """Build the ``condor_history`` that asks about jobs that have left the queue."""
        return commands.history(ids, status.STATE_ATTRIBUTES, ())

    def parse_history(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        """Read the jobs ``condor_history`` can tell about."""
        return _states(result, "condor_history")

    def kill_command(self, ids: Sequence[JobID]) -> Command:
        """Build the ``condor_rm``; it fails for a job that is no longer in the queue."""
        return commands.remove(ids, ())


def _states(result: CommandResult, tool: str) -> SchedulerStatusMap:
    if result.returncode != 0:
        raise ValueError(_reason(result, tool))
    return {
        status.job_id(ad): status.job_status(ad)
        for ad in status.parse_ads(result.stdout)
    }


def _reason(result: CommandResult, tool: str) -> str:
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return f"{tool}: {lines[-1]}" if lines else f"{tool} exited {result.returncode}"


# The type checker proves conformance.
_scheduler: type[Scheduler] = HTCondor
