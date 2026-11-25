"""The ARC backend: one module per capability, joined here.

Each operation is in a module of its own (``submit``, ``output``, ``cancel``,
``diagnostics``), and all of them share ``jobs``: which handles are this
backend's, and the states of their jobs. This class builds them, and checks the
backend is open before each operation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx

from intercede._backends.credentials import CredentialCache
from intercede._backends.monolith.arc.cancel import JobCanceller
from intercede._backends.monolith.arc.connection import Connection
from intercede._backends.monolith.arc.diagnostics import DiagnosticsReader
from intercede._backends.monolith.arc.jobs import Jobs
from intercede._backends.monolith.arc.output import OutputCollector
from intercede._backends.monolith.arc.submit import JobSubmitter
from intercede.auth.protocols import CredentialProvider
from intercede.config import ARCConfig
from intercede.exceptions import InterCEdeError
from intercede.models import (
    CopyVars,
    Diagnostics,
    JobHandle,
    JobOutput,
    OpOutcome,
    StatusMap,
    Submission,
    SubmissionSpec,
)
from intercede.protocols import (
    Diagnosable,
    JobBackend,
    Killable,
    OutputRetriever,
    Purgeable,
)


class ARCBackend:
    """An ARC CE: submit, status, output, kill, purge and live diagnostics."""

    def __init__(
        self,
        settings: ARCConfig,
        credentials: CredentialProvider,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Construction is cheap and does no I/O.
        self._settings = settings
        self._credentials = CredentialCache(credentials)  # a token per scope set
        self._connection = Connection(settings, self._credentials, transport)
        self._jobs = Jobs(settings.endpoint, self._connection)
        self._submitter = JobSubmitter(settings, self._jobs, self._connection)
        self._collector = OutputCollector(settings, self._jobs, self._connection)
        self._canceller = JobCanceller(self._jobs)
        self._reader = DiagnosticsReader(self._jobs, self._connection)
        self._closed = False

    @classmethod
    def from_settings(
        cls, settings: ARCConfig, credentials: CredentialProvider
    ) -> Self:
        """Build the backend from its settings; how the registry constructs it.

        Args:
            settings: The CE's validated configuration.
            credentials: The consumer's provider, asked for a token when an
                operation first needs one.

        Returns:
            A new backend, not yet opened.
        """
        return cls(settings, credentials)

    @property
    def resource(self) -> str:
        """The CE this backend talks to; the ``resource`` of its job handles."""
        return self._settings.endpoint

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._closed = False
        return self  # it connects at the first request

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Idempotent. Cached tokens are dropped; they were never written anywhere.
        self._closed = True
        await self._connection.close()
        self._credentials.clear()

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

    async def get_diagnostics(
        self, handles: Sequence[JobHandle], filename: str | None = None
    ) -> Mapping[JobHandle, OpOutcome[Diagnostics]]:
        """Read the end of a file in each job's session directory, the last MiB.

        ``None`` reads the job's standard output.
        """
        self._check_usable()
        return await self._reader.get_diagnostics(handles, filename)

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
_backend: type[JobBackend] = ARCBackend
_retriever: type[OutputRetriever] = ARCBackend
_killable: type[Killable] = ARCBackend
_purgeable: type[Purgeable] = ARCBackend
_diagnosable: type[Diagnosable] = ARCBackend
_built_by_registry: Callable[[ARCConfig, CredentialProvider], JobBackend] = (
    ARCBackend.from_settings
)
