"""The resource's jobs as every operation sees them: which are ours, and their states.

Handles come from the consumer's storage and their values end up on command
lines and in paths on the resource, so ``owns`` checks one before it is used.
Every command runs through ``run``, and every path on the resource is built
here, from the checked values (see ``layout`` for the files themselves).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable, Sequence

from intercede._backends.batch.protocols import (
    Command,
    CommandResult,
    Scheduler,
    SchedulerStatusMap,
    Transport,
)
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.exceptions import ResourceUnavailableError
from intercede.models import JobHandle, JobID, JobStatus, StatusMap

BACKEND = "batch"
COMMAND_TIMEOUT_S = 120  # every command but copying files, which uses the limits
CHUNK = 100  # job IDs per command line, so a poll of thousands of jobs stays bounded

_SUBMISSION = re.compile(r"[0-9a-f]{32}")
_COPY = re.compile(r"[0-9]+")


class Jobs:
    """The jobs on one resource, and the commands every operation runs there."""

    def __init__(
        self,
        settings: BatchLocalConfig | BatchSSHConfig,
        transport: Transport,
        scheduler: Scheduler,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._scheduler = scheduler

    @property
    def resource(self) -> str:
        """The resource the jobs run on; the ``resource`` of their handles."""
        return self._transport.resource

    def owns(self, handle: JobHandle) -> bool:
        """Tell whether a stored handle is one of this backend's, and well-formed.

        Handles come from the consumer's storage and their values end up on
        command lines and in paths on the resource, so they are checked.
        """
        return (
            handle.backend == BACKEND
            and handle.resource == self.resource
            and self._scheduler.is_job_id(handle.id)
            and _SUBMISSION.fullmatch(handle.routing.get("submission", "")) is not None
            and _COPY.fullmatch(handle.routing.get("copy", "")) is not None
        )

    def ids(self, handles: Iterable[JobHandle]) -> list[JobID]:
        """Return the IDs to ask the scheduler about: ours, each once."""
        return list(dict.fromkeys(handle.id for handle in handles if self.owns(handle)))

    async def status(self, handles: Sequence[JobHandle]) -> StatusMap:
        """Return the status of each job, in the caller's order."""
        states = await self.states(self.ids(handles))
        return {
            handle: states.get(handle.id, JobStatus.UNKNOWN)
            if self.owns(handle)
            else JobStatus.UNKNOWN
            for handle in handles
        }

    async def states(self, ids: Sequence[JobID]) -> dict[JobID, JobStatus]:
        """Ask the queue about jobs, and the history about those it does not know."""
        scheduler = self._scheduler
        states: dict[JobID, JobStatus] = {}
        for chunk in chunks(ids):
            states.update(
                await self._ask(scheduler.status_command, scheduler.parse_status, chunk)
            )
        for chunk in chunks([i for i in ids if i not in states]):
            states.update(
                await self._ask(
                    scheduler.history_command, scheduler.parse_history, chunk
                )
            )
        return {i: state for i, state in states.items() if state != JobStatus.UNKNOWN}

    async def run(self, argv: Command) -> CommandResult:
        """Run a command on the resource."""
        return await self._transport.run(argv, timeout=COMMAND_TIMEOUT_S)

    def submission_dir(self, submission: str) -> str:
        """Return the directory of a submission on the resource."""
        return posixpath.join(self._settings.workdir, submission)

    async def _ask(
        self,
        command: Callable[[Sequence[JobID]], Command],
        parse: Callable[[CommandResult, Sequence[JobID]], SchedulerStatusMap],
        ids: Sequence[JobID],
    ) -> SchedulerStatusMap:
        result = await self.run(command(ids))
        try:
            return parse(result, ids)
        except ValueError as exc:
            raise ResourceUnavailableError(
                f"asking {self.resource} about jobs failed: {exc}"
            ) from exc


def chunks(ids: Sequence[JobID]) -> list[Sequence[JobID]]:
    """Split IDs into groups of at most ``CHUNK``, one command line each."""
    return [ids[start : start + CHUNK] for start in range(0, len(ids), CHUNK)]
