"""The ARC backend: one module per capability, joined here.

Submitting is in a module of its own (``submit``), and it shares ``jobs``
with status: which handles are this backend's, and the states of their
jobs. This class builds them, and checks the
backend is open before each operation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Self

import httpx

from intercede._backends.credentials import CredentialCache
from intercede._backends.monolith.arc.connection import Connection
from intercede._backends.monolith.arc.jobs import Jobs
from intercede._backends.monolith.arc.submit import JobSubmitter
from intercede.auth.protocols import CredentialProvider
from intercede.config import ARCConfig
from intercede.exceptions import InterCEdeError
from intercede.models import (
    CopyVars,
    JobHandle,
    StatusMap,
    Submission,
    SubmissionSpec,
)
from intercede.protocols import JobBackend


class ARCBackend:
    """An ARC CE: submit and status.

    It has no live diagnostics (issue #22); ``Diagnosable`` is absent on purpose.
    """

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


# The type checker proves conformance.
_backend: type[JobBackend] = ARCBackend
_built_by_registry: Callable[[ARCConfig, CredentialProvider], JobBackend] = (
    ARCBackend.from_settings
)
