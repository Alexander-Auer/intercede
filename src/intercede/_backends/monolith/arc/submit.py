"""Submitting to an ARC CE: every copy a job, all created in one request.

The CE answers per description, so a copy it refuses is a failure while the
others are submitted. The files a job needs from this host (the executable,
local inputs, secrets) are uploaded to its session directory once it exists;
ARC starts the job when all of them are there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from intercede._backends._arc import rest
from intercede._backends._arc.description import render
from intercede._backends.common import copies_of
from intercede._backends.credentials import CANCEL_SCOPES, SUBMIT_SCOPES
from intercede._backends.monolith.arc import staging
from intercede._backends.monolith.arc.connection import PARALLEL_TRANSFERS, Connection
from intercede._backends.monolith.arc.jobs import BACKEND, Jobs
from intercede.config import ARCConfig
from intercede.exceptions import (
    InterCEdeError,
    ResourceUnavailableError,
    SpecificationRejectedError,
)
from intercede.models import CopyVars, JobHandle, JobID, Submission, SubmissionSpec

# Creating a job and uploading its files are two actions to ARC: with the WLCG
# token profile, writing to a session directory needs "compute.modify".
_CREATE_SCOPES = SUBMIT_SCOPES | {"compute.modify"}


class JobSubmitter:
    """Submits a specification's copies to the CE."""

    def __init__(self, settings: ARCConfig, jobs: Jobs, connection: Connection) -> None:
        self._settings = settings
        self._jobs = jobs
        self._connection = connection

    async def submit(
        self, spec: SubmissionSpec, copies: int | Sequence[CopyVars] = 1
    ) -> Submission:
        """Submit the copies; see ``Submitter.submit``."""
        copy_vars = copies_of(spec, copies)
        if not copy_vars:
            return Submission(handles={})
        resource = self._jobs.resource
        launcher = self._settings.container_launcher
        staging.check(spec, copy_vars, launcher, resource)
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
            index: JobHandle(backend=BACKEND, resource=resource, id=JobID(job_id))
            for index, job_id in created.items()
            if index not in stranded
        }
        if not handles:
            # Nothing was submitted: the whole operation failed.
            first = next(iter(stranded.values()), None)
            if first is not None:
                raise first
            raise ResourceUnavailableError(
                f"{resource} submitted no copy: {failures[0]}"
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
