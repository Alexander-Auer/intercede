"""The contract operations of the ARC backend."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import quote

import httpx

from intercede._backends._arc import rest, status
from intercede._backends._arc.description import read_kept, render
from intercede._backends.common import ACTIVE, copies_of
from intercede._backends.credentials import (
    CANCEL_SCOPES,
    READ_SCOPES,
    SUBMIT_SCOPES,
    CredentialCache,
)
from intercede._backends.monolith.arc import collect, staging
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
    Diagnostics,
    JobHandle,
    JobID,
    JobOutput,
    JobStatus,
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

BACKEND = "arc"
PARALLEL_TRANSFERS = 4
# How much of a file get_diagnostics() reads: its end, where a running job writes.
DIAGNOSTICS_BYTES = 2**20

# Creating a job and uploading its files are two actions to ARC: with the WLCG
# token profile, writing to a session directory needs "compute.modify".
_CREATE_SCOPES = SUBMIT_SCOPES | {"compute.modify"}


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

    # -- OutputRetriever ---------------------------------------------------

    async def get_output(
        self, handles: Sequence[JobHandle], dest: Path
    ) -> Mapping[JobHandle, OpOutcome[JobOutput]]:
        self._check_usable()
        states = await self._states(self._ids(handles))
        results: dict[JobHandle, OpOutcome[JobOutput]] = {}
        ready: dict[str, list[JobHandle]] = {}  # each job once
        for handle in handles:
            state = states.get(handle.id) if self._owns(handle) else None
            if state is None:
                results[handle] = _not_collected("unknown job")
            elif state in ACTIVE:
                results[handle] = _not_collected("job has not finished")
            else:
                ready.setdefault(handle.id, []).append(handle)

        limit = asyncio.Semaphore(PARALLEL_TRANSFERS)

        async def fetch(job_id: str, same: list[JobHandle]) -> None:
            async with limit:
                outcome = await self._collect(job_id, dest)
            results.update(dict.fromkeys(same, outcome))

        await asyncio.gather(*(fetch(job_id, same) for job_id, same in ready.items()))
        return {handle: results[handle] for handle in handles}  # in the caller's order

    async def _collect(self, job_id: str, dest: Path) -> OpOutcome[JobOutput]:
        """Copy what a finished job's specification keeps into ``dest``."""
        target = dest / quote(self.resource, safe="") / job_id
        timeout = self._settings.transfer_timeout_s
        try:
            async with asyncio.timeout(timeout):
                description = await self._connection.read(
                    rest.diagnose_path(job_id, "description"), READ_SCOPES
                )
                names = await self._walk(job_id)
                if description is None or names is None:
                    return _not_collected("the job was purged; its output is gone")
                selection = collect.select(
                    names,
                    read_kept(description.decode(errors="replace")),
                    max_files=self._settings.max_output_files,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                return await self._copy(job_id, selection, target)
        except TimeoutError:
            return _not_collected(f"collecting took longer than {timeout} s")
        except (ValueError, OSError, ResourceUnavailableError) as exc:
            return _not_collected(str(exc))

    async def _walk(self, job_id: str) -> list[str] | None:
        """List every file of a job's session directory, or None when it is gone."""
        names: list[str] = []
        directories = [""]
        while directories:
            directory = directories.pop()
            raw = await self._connection.read(
                rest.session_path(job_id, directory), READ_SCOPES
            )
            if raw is None:
                if not directory:
                    return None
                continue  # removed while it was being listed
            try:
                listing = rest.parse_listing(raw.decode())
            except (UnicodeDecodeError, ValueError):
                raise ValueError("the CE sent a listing that is not ARC's") from None
            names += (collect.child(directory, name) for name in listing.files)
            directories += (
                collect.child(directory, name) + "/" for name in listing.dirs
            )
        return names

    async def _copy(
        self, job_id: str, selection: collect.Selection, target: Path
    ) -> OpOutcome[JobOutput]:
        """Copy into a new directory and put it in place only when all of it arrived."""
        partial = Path(tempfile.mkdtemp(prefix=".partial-", dir=target.parent))
        allowed = self._settings.max_output_bytes
        left = allowed

        async def fetch(path: str, local: Path) -> Path | None:
            nonlocal left
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                written = await self._connection.download(
                    path, local, left, READ_SCOPES
                )
            except ValueError:
                raise ValueError(f"output larger than {allowed} bytes") from None
            if written is None:
                return None
            left -= written
            return target / local.relative_to(partial)

        async def member(name: str, local: Path) -> Path:
            path = await fetch(rest.session_path(job_id, name), local)
            if path is None:
                raise ValueError(f"{name} left the job's directory while it was copied")
            return path

        try:
            streams: dict[str, Path | None] = {}
            for field in ("stdout", "stderr"):
                if (name := getattr(selection, field)) is not None:
                    streams[field] = await member(name, partial / field)
            # The CE's log of the job: how it went through ARC and the batch system.
            streams["log"] = await fetch(
                rest.diagnose_path(job_id, "errors"), partial / "log"
            )
            files = {
                name: await member(name, partial / "files" / name)
                for name in selection.files
            }
            shutil.rmtree(target, ignore_errors=True)  # a previous collection
            partial.rename(target)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
        return OpOutcome[JobOutput](ok=True, value=JobOutput(files=files, **streams))

    # -- Diagnosable -------------------------------------------------------

    async def get_diagnostics(
        self, handles: Sequence[JobHandle], filename: str | None = None
    ) -> Mapping[JobHandle, OpOutcome[Diagnostics]]:
        """Read the end of a file in each job's session directory, the last MiB.

        The session directory is the job's working directory, and the CE serves
        it while the job runs. ``None`` reads the job's standard output.
        """
        self._check_usable()
        states = await self._states(self._ids(handles))
        results: dict[JobHandle, OpOutcome[Diagnostics]] = {}
        known: dict[str, list[JobHandle]] = {}
        for handle in handles:
            if self._owns(handle) and handle.id in states:
                known.setdefault(handle.id, []).append(handle)
            else:
                results[handle] = OpOutcome[Diagnostics](ok=False, reason="unknown job")

        limit = asyncio.Semaphore(PARALLEL_TRANSFERS)

        async def read(job_id: str, same: list[JobHandle]) -> None:
            async with limit:
                outcome = await self._diagnose(job_id, filename)
            results.update(dict.fromkeys(same, outcome))

        await asyncio.gather(*(read(job_id, same) for job_id, same in known.items()))
        return {handle: results[handle] for handle in handles}

    async def _diagnose(
        self, job_id: str, filename: str | None
    ) -> OpOutcome[Diagnostics]:
        try:
            name = filename
            if name is None:
                description = await self._connection.read(
                    rest.diagnose_path(job_id, "description"), READ_SCOPES
                )
                if description is None:
                    return _not_read("the job was purged")
                name = read_kept(description.decode(errors="replace")).stdout
            if not name:
                return _not_read("name a file in the job's directory")
            content = await self._connection.tail(
                rest.session_path(job_id, name), DIAGNOSTICS_BYTES, READ_SCOPES
            )
        except (ValueError, ResourceUnavailableError) as exc:
            return _not_read(str(exc))
        if content is None:
            return _not_read(f"the job's directory has no file {name!r}")
        diagnostics = Diagnostics(
            filename=filename or "stdout", content=content.decode(errors="replace")
        )
        return OpOutcome[Diagnostics](ok=True, value=diagnostics)

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


def _not_collected(reason: str) -> OpOutcome[JobOutput]:
    return OpOutcome[JobOutput](ok=False, reason=reason)


def _not_read(reason: str) -> OpOutcome[Diagnostics]:
    return OpOutcome[Diagnostics](ok=False, reason=reason)


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
_retriever: type[OutputRetriever] = ARCBackend
_killable: type[Killable] = ARCBackend
_purgeable: type[Purgeable] = ARCBackend
_diagnosable: type[Diagnosable] = ARCBackend
_built_by_registry: Callable[[ARCConfig, CredentialProvider], JobBackend] = (
    ARCBackend.from_settings
)
