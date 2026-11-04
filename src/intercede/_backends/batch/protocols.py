"""Internal collaborator protocols of the composed backend.

Shapes only: transports and schedulers are implemented in later issues. Moving
a job's files is neither's job alone: the backend does it, with nothing but
``Transport.put`` and ``get``, so it is not a third collaborator (``Stager``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict

from intercede.models import JobID, JobStatus, SubmissionSpec

Command = Sequence[str]  # an argv: only argv and staged files ever cross the transport
# One resource, so its own IDs are unique: no need for handles here.
SchedulerStatusMap = Mapping[JobID, JobStatus]


class CommandResult(BaseModel):
    """The outcome of running one command on the resource."""

    model_config = ConfigDict(frozen=True)

    returncode: int
    stdout: str
    stderr: str


class SubmitCommand(BaseModel):
    """What queues a submission: a command, and the files it needs on the resource."""

    model_config = ConfigDict(frozen=True)

    argv: Command
    files: Mapping[str, str] = {}  # name in the working directory -> content


@runtime_checkable
class Transport(Protocol):
    """How InterCEde reaches a resource: local shell, SSH, ...

    Transports hold connections, so they share the backend lifecycle: cheap
    construction with no I/O, and an idempotent close.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def run(self, argv: Command) -> CommandResult:
        """Run one command on the resource and wait for it to finish.

        Args:
            argv: The program and its arguments. Never passed through a shell.

        Returns:
            The exit code and everything the command printed. A non-zero exit
            code is returned, not raised: the scheduler decides what it means.

        Raises:
            ResourceUnavailableError: The resource could not be reached.
            AuthenticationError: The resource rejected the credentials.

        """
        ...

    async def put(self, local: Path, remote: str) -> None:
        """Copy a file or directory from this host to the resource.

        Args:
            local: The file or directory on this host.
            remote: Its destination path on the resource.

        Raises:
            ResourceUnavailableError: The copy failed.

        """
        ...

    async def get(self, remote: str, local: Path) -> None:
        """Copy a file or directory from the resource to this host.

        Args:
            remote: The file or directory on the resource.
            local: Its destination path on this host.

        Raises:
            ResourceUnavailableError: The copy failed.

        """
        ...


@runtime_checkable
class Scheduler(Protocol):
    """How work is queued: builds commands locally and parses their output.

    A scheduler never runs anything itself; the backend runs its commands
    through the transport. Its methods are pure, so they are tested without a
    resource. It never sees a copy's environment or secrets: the backend stages
    a script that starts a copy (see ``layout``), and all a scheduler does is
    queue that script once per copy.

    The parsers receive the whole result, since whether an exit code is a
    failure depends on the command: a tool that says it knows no such job has
    answered the question. A parser raises ``ValueError`` for a command that
    failed or printed something unreadable, which fails the whole operation.

    Attributes:
        stages_own_files: Whether the scheduler moves the job's files itself.
            When False, the backend copies them with the transport first.

    """

    stages_own_files: bool

    def is_job_id(self, value: str) -> bool:
        """Tell whether a stored ID can be one of this scheduler's.

        IDs come back from stored handles and end up on command lines, so one
        is checked first: a malformed one could pass as an option, or make a
        tool refuse the valid IDs asked about with it.

        Args:
            value: The ID to check.

        Returns:
            True when it has the form of a job ID.

        """
        ...

    def submit_command(
        self, spec: SubmissionSpec, count: int, workdir: str
    ) -> SubmitCommand:
        """Build what queues ``count`` copies of ``spec``.

        Args:
            spec: What every copy shares; the scheduler reads its resources.
            count: How many copies to queue.
            workdir: The directory on the resource that holds the submission
                (see ``layout``); the returned files are staged there first.

        Returns:
            The command to run through the transport, and its files.

        Raises:
            SpecificationRejectedError: The scheduler cannot express ``spec``.

        """
        ...

    def parse_submit(self, result: CommandResult, count: int) -> Sequence[JobID]:
        """Read the job IDs from the outcome of the submit command.

        Args:
            result: The outcome of the submit command.
            count: How many copies it queued.

        Returns:
            ``count`` IDs, in copy order.

        Raises:
            ValueError: Nothing was queued, or the output cannot be read.

        """
        ...

    def status_command(self, ids: Sequence[JobID]) -> Command:
        """Build the command that asks the queue about the given jobs.

        Args:
            ids: The jobs to ask about.

        Returns:
            The command to run through the transport.

        """
        ...

    def parse_status(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        """Read the jobs' states from the outcome of the status command.

        Args:
            result: The outcome of the status command.
            ids: The jobs that were asked about.

        Returns:
            The status of the jobs the queue knows. The backend asks the
            history about the others.

        Raises:
            ValueError: The command failed, or its output cannot be read.

        """
        ...

    def history_command(self, ids: Sequence[JobID]) -> Command:
        """Build the command that asks about jobs that have left the queue.

        Args:
            ids: The jobs to ask about.

        Returns:
            The command to run through the transport.

        """
        ...

    def parse_history(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        """Read the jobs' states from the outcome of the history command.

        Args:
            result: The outcome of the history command.
            ids: The jobs that were asked about.

        Returns:
            The status of the jobs the history knows; the others are
            ``UNKNOWN`` to the consumer.

        Raises:
            ValueError: The command failed, or its output cannot be read.

        """
        ...

    def kill_command(self, ids: Sequence[JobID]) -> Command:
        """Build the command that cancels the given jobs.

        Args:
            ids: The jobs to cancel.

        Returns:
            The command to run through the transport.

        """
        ...
