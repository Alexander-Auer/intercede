"""The composed backend: one transport and one scheduler, and nothing about either.

It holds what every combination shares: how a submission is staged and found
again, which job is whose, and what a capability does with the scheduler's
commands. It never asks which transport or scheduler it has, so N transports and
M schedulers need N + M classes, not N x M. See ``layout`` for the files it keeps
on the resource, which are all it keeps: it remembers no jobs.

Each operation is in a module of its own (``submit``, ``output``, ``cancel``), and
all of them share ``jobs``: which handles are this backend's, and the states of
their jobs. This class builds them, and checks the backend is open before each
operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from intercede._backends.batch.cancel import JobCanceller
from intercede._backends.batch.jobs import Jobs
from intercede._backends.batch.output import OutputCollector
from intercede._backends.batch.protocols import Scheduler, Transport
from intercede._backends.batch.submit import JobSubmitter
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.exceptions import InterCEdeError
from intercede.models import (
    CopyVars,
    JobHandle,
    JobOutput,
    OpOutcome,
    StatusMap,
    Submission,
    SubmissionSpec,
)
from intercede.protocols import JobBackend, Killable, OutputRetriever, Purgeable


class BatchBackend:
    """A batch system: submit, status, output, kill and purge, through a transport."""

    def __init__(
        self,
        settings: BatchLocalConfig | BatchSSHConfig,
        transport: Transport,
        scheduler: Scheduler,
    ) -> None:
        # Construction is cheap and does no I/O.
        self._transport = transport
        self._jobs = Jobs(settings, transport, scheduler)
        self._submitter = JobSubmitter(settings, self._jobs, transport, scheduler)
        self._collector = OutputCollector(settings, self._jobs, transport)
        self._canceller = JobCanceller(self._jobs, scheduler)
        self._closed = False

    @classmethod
    def from_settings(
        cls,
        settings: BatchLocalConfig | BatchSSHConfig,
        *,
        transport: Transport,
        scheduler: Scheduler,
    ) -> Self:
        """Build the backend from its parts; how the registry constructs it.

        Args:
            settings: The batch backend's validated configuration.
            transport: How the resource is reached, not yet connected.
            scheduler: How work is queued on it.

        Returns:
            A new backend, not yet opened.
        """
        return cls(settings, transport, scheduler)

    @property
    def resource(self) -> str:
        """The resource this backend talks to; the ``resource`` of its job handles."""
        return self._transport.resource

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self._transport.__aenter__()
        self._closed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Idempotent, like closing the transport.
        self._closed = True
        await self._transport.__aexit__(exc_type, exc, tb)

    def _check_usable(self) -> None:
        if self._closed:
            raise InterCEdeError("backend is closed")

    # -- the capabilities --------------------------------------------------

    async def submit(
        self, spec: SubmissionSpec, copies: int | Sequence[CopyVars] = 1
    ) -> Submission:
        self._check_usable()
        return await self._submitter.submit(spec, copies)

    async def get_status(self, handles: Sequence[JobHandle]) -> StatusMap:
        self._check_usable()
        return await self._jobs.status(handles)

    async def get_output(
        self, handles: Sequence[JobHandle], dest: Path
    ) -> Mapping[JobHandle, OpOutcome[JobOutput]]:
        self._check_usable()
        return await self._collector.get_output(handles, dest)

    async def kill(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        self._check_usable()
        return await self._canceller.kill(handles)

    async def purge(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        self._check_usable()
        return await self._canceller.purge(handles)


# The type checker proves conformance.
_backend: type[JobBackend] = BatchBackend
_output: type[OutputRetriever] = BatchBackend
_kill: type[Killable] = BatchBackend
_purge: type[Purgeable] = BatchBackend
