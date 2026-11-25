"""Submitting through a transport: stage the files here, copy them over, queue the jobs.

A submission is a directory on the resource (see ``layout``), staged in full on
this host first, so the scheduler only ever queues jobs whose files are there.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from intercede._backends.batch import staging
from intercede._backends.batch.jobs import BACKEND, Jobs
from intercede._backends.batch.protocols import Scheduler, Transport
from intercede._backends.common import copies_of
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.exceptions import (
    InterCEdeError,
    ResourceUnavailableError,
    SpecificationRejectedError,
)
from intercede.models import CopyVars, JobHandle, Submission, SubmissionSpec


class JobSubmitter:
    """Submits a specification's copies to the resource's scheduler."""

    def __init__(
        self,
        settings: BatchLocalConfig | BatchSSHConfig,
        jobs: Jobs,
        transport: Transport,
        scheduler: Scheduler,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._transport = transport
        self._scheduler = scheduler

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

        submission = uuid.uuid4().hex
        workdir = self._jobs.submission_dir(submission)
        command = self._scheduler.submit_command(spec, len(copy_vars), workdir)
        with tempfile.TemporaryDirectory(prefix="intercede-") as staged:
            local = Path(staged) / submission
            try:
                await asyncio.to_thread(
                    staging.stage, spec, copy_vars, launcher, command.files, local
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
        result = await self._jobs.run(command.argv)
        try:
            ids = self._scheduler.parse_submit(result, len(copy_vars))
        except ValueError as exc:
            if result.returncode != 0:  # nothing was queued, so nothing needs the files
                await self._discard(workdir)
            raise ResourceUnavailableError(
                f"submitting to {resource} failed: {exc}"
            ) from exc
        return Submission(
            handles={
                index: JobHandle(
                    backend=BACKEND,
                    resource=resource,
                    id=job_id,
                    routing={"submission": submission, "copy": str(index)},
                )
                for index, job_id in enumerate(ids)
            }
        )

    async def _discard(self, workdir: str) -> None:
        """Remove what was staged for a submission that did not happen, if that can be done."""
        with suppress(InterCEdeError):
            await self._jobs.run(["rm", "-rf", workdir])
