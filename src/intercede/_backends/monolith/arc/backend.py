"""The contract operations of the ARC backend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import TracebackType
from typing import Self

import httpx

from intercede._backends._arc import rest, status
from intercede._backends._arc.description import render
from intercede._backends.common import copies_of
from intercede._backends.credentials import (
    CANCEL_SCOPES,
    READ_SCOPES,
    SUBMIT_SCOPES,
    CredentialCache,
)
from intercede._backends.monolith.arc import staging
from intercede._backends.monolith.arc.connection import Connection
from intercede.auth.protocols import CredentialProvider
from intercede.config import ARCConfig
from intercede.exceptions import (
    InterCEdeError,
    ResourceUnavailableError,
    SpecificationRejectedError,
)
from intercede.models import (
    CopyVars,
    JobHandle,
    JobID,
    JobStatus,
    StatusMap,
    Submission,
    SubmissionSpec,
)
from intercede.protocols import JobBackend

BACKEND = "arc"
PARALLEL_TRANSFERS = 4

# Creating a job and uploading its files are two actions to ARC: with the WLCG
# token profile, writing to a session directory needs "compute.modify".
_CREATE_SCOPES = SUBMIT_SCOPES | {"compute.modify"}


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

    # -- Submitter ---------------------------------------------------------

    async def submit(
        self, spec: SubmissionSpec, copies: int | Sequence[CopyVars] = 1
    ) -> Submission:
        self._check_usable()
        copy_vars = copies_of(spec, copies)
        if not copy_vars:
            return Submission(handles={})
        launcher = self._settings.container_launcher
        staging.check(spec, copy_vars, launcher, self.resource)
        shared = staging.uploads(spec)
        plans = staging.plans(spec, copy_vars, launcher, self._settings.queue)
        try:
            description = render(plans)
        except ValueError as exc:
            raise SpecificationRejectedError(str(exc)) from exc

        answers = await self._connection.create(description, _CREATE_SCOPES)
        created: dict[int, str] = {}
        failures: dict[int, str] = {}
        for index in range(len(copy_vars)):
            answer = answers[index] if index < len(answers) else None
            if answer is None:
                failures[index] = "the CE did not answer for this copy"
            elif not answer.ok or answer.id is None or not rest.is_job_id(answer.id):
                failures[index] = (
                    f"the CE refused it: {answer.code} {answer.reason}".rstrip()
                )
            else:
                created[index] = answer.id

        # ARC starts a job once every file it waits for is in its session directory.
        limit = asyncio.Semaphore(PARALLEL_TRANSFERS)

        async def stage(index: int, job_id: str) -> InterCEdeError | None:
            secrets = copy_vars[index].secrets
            files = {**shared, **{n: s.get_secret_value() for n, s in secrets.items()}}
            try:
                for name, content in files.items():
                    async with limit:
                        await self._connection.upload(
                            job_id, name, content, _CREATE_SCOPES
                        )
            except InterCEdeError as exc:
                return exc
            return None

        staged = await asyncio.gather(*(stage(i, j) for i, j in created.items()))
        stranded = {
            index: error
            for index, error in zip(created, staged, strict=True)
            if error is not None
        }
        failures.update(await self._abandon(created, stranded))

        handles = {
            index: JobHandle(backend=BACKEND, resource=self.resource, id=JobID(job_id))
            for index, job_id in created.items()
            if index not in stranded
        }
        if not handles:
            # Nothing was submitted: the whole operation failed.
            first = next(iter(stranded.values()), None)
            if first is not None:
                raise first
            raise ResourceUnavailableError(
                f"{self.resource} submitted no copy: {failures[0]}"
            )
        return Submission(handles=handles, failures=failures)

    async def _abandon(
        self, created: Mapping[int, str], stranded: Mapping[int, InterCEdeError]
    ) -> dict[int, str]:
        """Cancel the jobs whose files could not all be uploaded.

        Such a job would wait for its files until the CE gives up on it. It is
        not the consumer's, since it gets no handle, so it is cancelled here.

        Returns:
            The failure of each copy, saying whether its job is gone.
        """
        if not stranded:
            return {}
        ids = [created[index] for index in stranded]
        try:
            answers = await self._connection.jobs("kill", ids, CANCEL_SCOPES)
            cancelled = {answer.id for answer in answers if answer.ok}
        except InterCEdeError:
            cancelled = set()
        return {
            index: f"uploading its files failed: {error}; "
            + (
                "the job was cancelled"
                if created[index] in cancelled
                else f"job {created[index]} could not be cancelled"
            )
            for index, error in stranded.items()
        }

    # -- StatusReporter ----------------------------------------------------

    async def get_status(self, handles: Sequence[JobHandle]) -> StatusMap:
        self._check_usable()
        ids = self._ids(handles)
        states: dict[str, JobStatus] = {}
        if ids:
            for answer in await self._connection.jobs("status", ids, READ_SCOPES):
                # A job the CE does not know is answered with 404, and no state.
                if answer.ok and answer.id is not None and answer.state is not None:
                    states[answer.id] = status.job_status(answer.state)
        return {
            handle: states.get(handle.id, JobStatus.UNKNOWN)
            if self._owns(handle)
            else JobStatus.UNKNOWN
            for handle in handles
        }

    # -- helpers -------------------------------------------------------------

    def _check_usable(self) -> None:
        if self._closed:
            raise InterCEdeError("backend is closed")

    def _owns(self, handle: JobHandle) -> bool:
        return (
            handle.backend == BACKEND
            and handle.resource == self.resource
            and rest.is_job_id(handle.id)
        )

    def _ids(self, handles: Iterable[JobHandle]) -> list[str]:
        """Return the ids to ask the CE about: ours, well-formed, each once.

        Ids come from stored handles, so they are checked before they reach a URL.
        """
        return list(
            dict.fromkeys(handle.id for handle in handles if self._owns(handle))
        )


# The type checker proves conformance.
_backend: type[JobBackend] = ARCBackend
_built_by_registry: Callable[[ARCConfig, CredentialProvider], JobBackend] = (
    ARCBackend.from_settings
)
