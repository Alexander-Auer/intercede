"""The contract operations of the ARC backend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import TracebackType
from typing import Self

import httpx

from intercede._backends._arc import rest, status
from intercede._backends._arc.description import render
from intercede._backends.common import ACTIVE, copies_of
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
    OpOutcome,
    StatusMap,
    Submission,
    SubmissionSpec,
)
from intercede.protocols import JobBackend, Killable, Purgeable

BACKEND = "arc"
PARALLEL_TRANSFERS = 4

# Creating a job and uploading its files are two actions to ARC: with the WLCG
# token profile, writing to a session directory needs "compute.modify".
_CREATE_SCOPES = SUBMIT_SCOPES | {"compute.modify"}


class ARCBackend:
    """An ARC CE: submit, status, kill and purge.

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
        states = await self._states(self._ids(handles))
        return {
            handle: states.get(handle.id, JobStatus.UNKNOWN)
            if self._owns(handle)
            else JobStatus.UNKNOWN
            for handle in handles
        }

    # -- Killable --------------------------------------------------------------

    async def kill(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        self._check_usable()
        # A-REX accepts a kill for a job that has ended too, and does nothing
        # with it: only the state tells a finished job from a cancelled one.
        states = await self._states(self._ids(handles))
        active = [job for job, state in states.items() if state in ACTIVE]
        killed = await self._ask("kill", active, CANCEL_SCOPES)
        outcomes: dict[JobHandle, OpOutcome[None]] = {}
        for handle in handles:
            if not self._owns(handle) or handle.id not in states:
                outcomes[handle] = _UNKNOWN_JOB
            elif handle.id in active:
                outcomes[handle] = _done(killed.get(handle.id))
            else:
                outcomes[handle] = OpOutcome[None](ok=True, reason="already finished")
        return outcomes

    # -- Purgeable -------------------------------------------------------------

    async def purge(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        self._check_usable()
        states = await self._states(self._ids(handles))
        active = [job for job, state in states.items() if state in ACTIVE]
        killed = await self._ask("kill", active, CANCEL_SCOPES)
        # A-REX keeps a clean of a job that is still active until it has ended,
        # so a job cancelled here goes once it has stopped. One that could not
        # be cancelled keeps its files.
        refused = {job for job in active if not _done(killed.get(job)).ok}
        cleaned = await self._ask(
            "clean", [job for job in states if job not in refused], CANCEL_SCOPES
        )
        outcomes: dict[JobHandle, OpOutcome[None]] = {}
        for handle in handles:
            if not self._owns(handle):
                outcomes[handle] = _UNKNOWN_JOB
            elif handle.id not in states:
                # Purged before, or never known: either way nothing is left.
                outcomes[handle] = _NOTHING_LEFT
            elif handle.id in refused:
                outcomes[handle] = _done(killed.get(handle.id))
            else:
                outcome = _done(cleaned.get(handle.id))
                if outcome.ok and handle.id in active:
                    outcome = OpOutcome[None](
                        ok=True, reason="cancelled; released once it has stopped"
                    )
                elif outcome.reason == _UNKNOWN_JOB.reason:
                    outcome = _NOTHING_LEFT
                outcomes[handle] = outcome
        return outcomes

    # -- helpers -------------------------------------------------------------

    def _check_usable(self) -> None:
        if self._closed:
            raise InterCEdeError("backend is closed")

    async def _ask(
        self, action: str, ids: Sequence[str], scopes: frozenset[str]
    ) -> dict[str, rest.JobAnswer]:
        """Ask the CE to do ``action`` for each job of ``ids``; its answers by job."""
        if not ids:
            return {}
        answers = await self._connection.jobs(action, ids, scopes)
        return {answer.id: answer for answer in answers if answer.id is not None}

    async def _states(self, ids: Sequence[str]) -> dict[str, JobStatus]:
        """Return the state of each job the CE knows; it answers 404 for the others."""
        answers = await self._ask("status", ids, READ_SCOPES)
        return {
            job: status.job_status(answer.state)
            for job, answer in answers.items()
            if answer.ok and answer.state is not None
        }

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


_UNKNOWN_JOB = OpOutcome[None](ok=False, reason="unknown job")
_NOTHING_LEFT = OpOutcome[None](
    ok=True, reason="the CE no longer knows the job; nothing is left to release"
)


def _done(answer: rest.JobAnswer | None) -> OpOutcome[None]:
    """Read the CE's answer to a kill or a clean of one job."""
    if answer is None:
        return OpOutcome[None](ok=False, reason="the CE did not answer for this job")
    if answer.ok:
        return OpOutcome[None](ok=True)
    if answer.code == httpx.codes.NOT_FOUND:
        return _UNKNOWN_JOB
    return OpOutcome[None](
        ok=False, reason=f"the CE refused it: {answer.code} {answer.reason}".rstrip()
    )


# The type checker proves conformance.
_backend: type[JobBackend] = ARCBackend
_killable: type[Killable] = ARCBackend
_purgeable: type[Purgeable] = ARCBackend
_built_by_registry: Callable[[ARCConfig, CredentialProvider], JobBackend] = (
    ARCBackend.from_settings
)
