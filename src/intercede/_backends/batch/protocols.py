"""Internal collaborator protocols of the composed backend.

Transports are implemented in ``transport``; schedulers in a later issue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict

from intercede.models import CopyVars, JobID, JobStatus, SubmissionSpec

Command = Sequence[str]  # an argv: only argv and staged files ever cross the transport
# One resource, so its own IDs are unique: no need for handles here.
SchedulerStatusMap = Mapping[JobID, JobStatus]


class CommandResult(BaseModel):
    """The outcome of running one command on the resource."""

    model_config = ConfigDict(frozen=True)

    returncode: int
    stdout: str
    stderr: str


@runtime_checkable
class Transport(Protocol):
    """How InterCEde reaches a resource: local shell, SSH, ...

    Transports hold connections, so they share the backend lifecycle: cheap
    construction with no I/O, and an idempotent close.

    Every transport behaves the same, so a backend never asks which one it has.
    Commands start in the account's home directory on the resource, and a
    relative path on the resource is relative to it. The caller picks every
    timeout: it knows whether a call queries a job or stages gigabytes.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def run(self, argv: Command, *, timeout: float) -> CommandResult:
        """Run one command on the resource and wait for it to finish.

        Args:
            argv: The program and its arguments. Each argument reaches the
                program exactly as given, whatever characters it holds.
            timeout: How long the command may take, in seconds.

        Returns:
            The exit code and everything the command printed. A non-zero exit
            code is returned, not raised: the scheduler decides what it means.
            As in a shell, a program that does not exist exits with 127, and
            one that cannot be executed with 126.

        Raises:
            ResourceUnavailableError: The resource could not be reached, or
                the command did not finish within ``timeout``.
            AuthenticationError: The resource rejected the credentials.

        """
        ...

    async def put(self, local: Path, remote: str, *, timeout: float) -> None:
        """Copy a file or directory from this host to the resource.

        ``remote`` is the path the copy gets, never a directory to copy into:
        an existing file there is replaced, and an existing directory receives
        the copied directory's contents. Missing parent directories are
        created, and permission bits are kept, so an executable stays one.

        Args:
            local: The file or directory on this host.
            remote: The path of the copy on the resource.
            timeout: How long the copy may take, in seconds.

        Raises:
            ResourceUnavailableError: The copy failed, or did not finish
                within ``timeout``.
            AuthenticationError: The resource rejected the credentials.

        """
        ...

    async def get(self, remote: str, local: Path, *, timeout: float) -> None:
        """Copy a file or directory from the resource to this host.

        The mirror of ``put``: ``local`` is the path the copy gets.

        Args:
            remote: The file or directory on the resource.
            local: The path of the copy on this host.
            timeout: How long the copy may take, in seconds.

        Raises:
            ResourceUnavailableError: The copy failed, or did not finish
                within ``timeout``.
            AuthenticationError: The resource rejected the credentials.

        """
        ...


@runtime_checkable
class Scheduler(Protocol):
    """How work is queued: builds commands locally and parses their output.

    A scheduler never runs anything itself; the backend runs its commands
    through the transport. Its methods are pure, so they are tested without a
    resource.

    Attributes:
        stages_own_files: Whether the scheduler moves the job's files itself.
            When False, the backend copies them with the transport first.

    """

    stages_own_files: bool

    def submit_command(
        self, spec: SubmissionSpec, copies: Sequence[CopyVars], workdir: str
    ) -> Command:
        """Build the command that queues every copy of ``spec``.

        Args:
            spec: What every copy shares.
            copies: One entry per copy, in copy order.
            workdir: The directory on the resource where the files were staged.

        Returns:
            The command to run through the transport.

        """
        ...

    def parse_submit(self, raw: str) -> Sequence[JobID]:
        """Read the job IDs from the output of the submit command.

        Args:
            raw: What the submit command printed.

        Returns:
            One ID per copy, in copy order.

        Raises:
            ValueError: ``raw`` does not hold one ID per copy.

        """
        ...

    def status_command(self, ids: Sequence[JobID]) -> Command:
        """Build the command that reports the state of the given jobs.

        Args:
            ids: The jobs to ask about.

        Returns:
            The command to run through the transport.

        """
        ...

    def parse_status(self, raw: str, ids: Sequence[JobID]) -> SchedulerStatusMap:
        """Read the jobs' states from the output of the status command.

        Args:
            raw: What the status command printed.
            ids: The jobs that were asked about.

        Returns:
            A status for every ID in ``ids``; an ID missing from ``raw`` is
            ``UNKNOWN``.

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
