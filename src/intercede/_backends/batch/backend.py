"""The composed backend: one transport and one scheduler, and nothing about either.

It holds what every combination shares: how a submission is staged and found
again, which job is whose, and what a capability does with the scheduler's
commands. It never asks which transport or scheduler it has, so N transports and
M schedulers need N + M classes, not N x M. See ``layout`` for the files it keeps
on the resource, which are all it keeps: it remembers no jobs.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Self

from intercede._backends.batch import job, layout
from intercede._backends.batch.protocols import (
    Command,
    CommandResult,
    Scheduler,
    SchedulerStatusMap,
    Transport,
)
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.exceptions import (
    ConfigurationError,
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

BACKEND = "batch"
COMMAND_TIMEOUT_S = 120  # every command but copying files, which uses the limits
CHUNK = 100  # job IDs per command line, so a poll of thousands of jobs stays bounded

_ACTIVE = frozenset({JobStatus.SUBMITTED, JobStatus.WAITING, JobStatus.RUNNING})
_SUBMISSION = re.compile(r"[0-9a-f]{32}")
_COPY = re.compile(r"[0-9]+")


class BatchBackend:
    """A batch system through a transport: submit, status, kill and purge."""

    def __init__(
        self,
        settings: BatchLocalConfig | BatchSSHConfig,
        transport: Transport,
        scheduler: Scheduler,
    ) -> None:
        # Construction is cheap and does no I/O.
        if scheduler.stages_own_files:
            raise ConfigurationError(
                f"the scheduler {settings.scheduler!r} moves a job's files itself, "
                "which the composed backend does not support yet"
            )
        self._settings = settings
        self._transport = transport
        self._scheduler = scheduler
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

    # -- Submitter ---------------------------------------------------------

    async def submit(
        self, spec: SubmissionSpec, copies: int | Sequence[CopyVars] = 1
    ) -> Submission:
        self._check_usable()
        copy_vars = [CopyVars()] * copies if isinstance(copies, int) else list(copies)
        if not copy_vars:
            return Submission(handles={})
        launcher = self._settings.container_launcher
        job.check(spec, copy_vars, launcher, self.resource)

        submission = uuid.uuid4().hex
        workdir = self._submission_dir(submission)
        command = self._scheduler.submit_command(spec, len(copy_vars), workdir)
        with tempfile.TemporaryDirectory(prefix="intercede-") as staging:
            local = Path(staging) / submission
            try:
                await asyncio.to_thread(
                    job.stage, spec, copy_vars, launcher, command.files, local
                )
            except ValueError as exc:  # a name no file can have
                raise SpecificationRejectedError(str(exc)) from exc
            except OSError as exc:
                raise ResourceUnavailableError(
                    f"preparing the job's files on this host failed: {exc}"
                ) from exc
            try:
                await self._transport.put(
                    local, workdir, timeout=self._settings.transfer_timeout_s
                )
            except ResourceUnavailableError:
                await self._discard(workdir)  # part of it may have arrived
                raise
        # If this one times out it may have queued the jobs, which need their files.
        result = await self._run(command.argv)
        try:
            ids = self._scheduler.parse_submit(result, len(copy_vars))
        except ValueError as exc:
            if result.returncode != 0:  # nothing was queued, so nothing needs the files
                await self._discard(workdir)
            raise ResourceUnavailableError(
                f"submitting to {self.resource} failed: {exc}"
            ) from exc
        return Submission(
            handles={
                index: JobHandle(
                    backend=BACKEND,
                    resource=self.resource,
                    id=job_id,
                    routing={"submission": submission, "copy": str(index)},
                )
                for index, job_id in enumerate(ids)
            }
        )

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

    # -- Killable / Purgeable ----------------------------------------------

    async def kill(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        self._check_usable()
        ids = self._ids(handles)
        states = await self._states(ids)
        # Only active jobs: a finished one has nothing left to cancel.
        refused = await self._cancel([i for i in ids if states.get(i) in _ACTIVE])
        results = {}
        for handle in handles:
            state = states.get(handle.id) if self._owns(handle) else None
            if state is None:
                results[handle] = OpOutcome[None](ok=False, reason="unknown job")
            elif handle.id in refused:
                results[handle] = OpOutcome[None](ok=False, reason=refused[handle.id])
            elif state in _ACTIVE:
                results[handle] = OpOutcome[None](ok=True)
            else:
                results[handle] = OpOutcome[None](ok=True, reason="already finished")
        return results

    async def purge(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        self._check_usable()
        ids = self._ids(handles)
        states = await self._states(ids)
        refused = await self._cancel([i for i in ids if states.get(i) in _ACTIVE])
        ours = [h for h in handles if self._owns(h) and h.id not in refused]
        failed = await self._remove(ours)
        results = {}
        for handle in handles:
            if not self._owns(handle):
                results[handle] = OpOutcome[None](ok=False, reason="unknown job")
            elif handle.id in refused or handle in failed:
                reason = refused.get(handle.id) or failed[handle]
                results[handle] = OpOutcome[None](ok=False, reason=reason)
            else:
                results[handle] = OpOutcome[None](ok=True)
        return results

    async def _cancel(self, ids: Sequence[JobID]) -> dict[JobID, str]:
        """Cancel jobs; return the reason for each that was not cancelled."""
        failures: dict[JobID, str] = {}
        for chunk in _chunks(ids):
            result = await self._run(self._scheduler.kill_command(chunk))
            if result.returncode != 0:
                failures.update(dict.fromkeys(chunk, _reason(result)))
        if not failures:
            return {}
        # A tool does not say which ID failed, so see what really happened: a
        # job may also have ended in the meantime.
        states = await self._states(list(failures))
        return {i: why for i, why in failures.items() if states.get(i) in _ACTIVE}

    async def _remove(self, handles: Sequence[JobHandle]) -> dict[JobHandle, str]:
        """Remove jobs' files; return the reason for each whose files could not be."""
        failed: dict[JobHandle, str] = {}
        unique = list(dict.fromkeys(handles))
        for start in range(0, len(unique), CHUNK):
            chunk = unique[start : start + CHUNK]
            result = await self._run(["rm", "-rf", *map(self._copy_dir, chunk)])
            if result.returncode != 0:
                failed.update(dict.fromkeys(chunk, _reason(result)))
        submissions = {h.routing["submission"] for h in unique if h not in failed}
        for submission in submissions:
            workdir = self._submission_dir(submission)
            # Fails while other copies remain, which then still need the inputs.
            emptied = await self._run(["rmdir", f"{workdir}/{layout.COPIES}"])
            if emptied.returncode == 0:
                await self._run(["rm", "-rf", workdir])
        return failed

    # -- talking to the resource -------------------------------------------

    async def _states(self, ids: Sequence[JobID]) -> dict[JobID, JobStatus]:
        """Ask the queue about jobs, and the history about those it does not know."""
        scheduler = self._scheduler
        states: dict[JobID, JobStatus] = {}
        for chunk in _chunks(ids):
            states.update(
                await self._ask(scheduler.status_command, scheduler.parse_status, chunk)
            )
        for chunk in _chunks([i for i in ids if i not in states]):
            states.update(
                await self._ask(
                    scheduler.history_command, scheduler.parse_history, chunk
                )
            )
        return {i: state for i, state in states.items() if state != JobStatus.UNKNOWN}

    async def _ask(
        self,
        command: Callable[[Sequence[JobID]], Command],
        parse: Callable[[CommandResult, Sequence[JobID]], SchedulerStatusMap],
        ids: Sequence[JobID],
    ) -> SchedulerStatusMap:
        result = await self._run(command(ids))
        try:
            return parse(result, ids)
        except ValueError as exc:
            raise ResourceUnavailableError(
                f"asking {self.resource} about jobs failed: {exc}"
            ) from exc

    async def _run(self, argv: Command) -> CommandResult:
        return await self._transport.run(argv, timeout=COMMAND_TIMEOUT_S)

    async def _discard(self, workdir: str) -> None:
        """Remove what was staged for a submission that did not happen, if that can be done."""
        with suppress(InterCEdeError):
            await self._run(["rm", "-rf", workdir])

    # -- helpers -----------------------------------------------------------

    def _check_usable(self) -> None:
        if self._closed:
            raise InterCEdeError("backend is closed")

    def _owns(self, handle: JobHandle) -> bool:
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

    def _ids(self, handles: Iterable[JobHandle]) -> list[JobID]:
        """Return the IDs to ask the scheduler about: ours, each once."""
        return list(
            dict.fromkeys(handle.id for handle in handles if self._owns(handle))
        )

    def _submission_dir(self, submission: str) -> str:
        return posixpath.join(self._settings.workdir, submission)

    def _copy_dir(self, handle: JobHandle) -> str:
        """Return a job's directory, built from checked values: see ``_owns``."""
        return posixpath.join(
            self._submission_dir(handle.routing["submission"]),
            layout.copy_dir(handle.routing["copy"]),
        )


def _chunks(ids: Sequence[JobID]) -> list[Sequence[JobID]]:
    return [ids[start : start + CHUNK] for start in range(0, len(ids), CHUNK)]


def _reason(result: CommandResult) -> str:
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f"exit code {result.returncode}"


# The type checker proves conformance.
_backend: type[JobBackend] = BatchBackend
_kill: type[Killable] = BatchBackend
_purge: type[Purgeable] = BatchBackend
