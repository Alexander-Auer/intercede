"""Collecting a finished job's output from the resource, through the transport.

What to copy is decided by ``collect`` from the job's manifest and a listing of
its directory, before a single file is copied.
"""

from __future__ import annotations

import asyncio
import posixpath
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError

from intercede._backends.batch import collect, layout, staging
from intercede._backends.batch.jobs import Jobs
from intercede._backends.batch.protocols import Transport
from intercede._backends.common import ACTIVE
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.exceptions import ResourceUnavailableError
from intercede.models import JobHandle, JobOutput, OpOutcome

PARALLEL_TRANSFERS = 4


class OutputCollector:
    """Copies the output of finished jobs into a directory of the caller's."""

    def __init__(
        self,
        settings: BatchLocalConfig | BatchSSHConfig,
        jobs: Jobs,
        transport: Transport,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._transport = transport

    async def get_output(
        self, handles: Sequence[JobHandle], dest: Path
    ) -> Mapping[JobHandle, OpOutcome[JobOutput]]:
        """Collect each finished job's output; see ``OutputRetriever.get_output``."""
        states = await self._jobs.states(self._jobs.ids(handles))
        results: dict[JobHandle, OpOutcome[JobOutput]] = {}
        ready: dict[JobHandle, None] = {}  # each once, in the caller's order
        for handle in handles:
            state = states.get(handle.id) if self._jobs.owns(handle) else None
            if state is None:
                results[handle] = _not_collected("unknown job")
            elif state in ACTIVE:
                results[handle] = _not_collected("job has not finished")
            else:
                ready[handle] = None

        limit = asyncio.Semaphore(PARALLEL_TRANSFERS)

        async def fetch(handle: JobHandle) -> None:
            async with limit:
                results[handle] = await self._collect(handle, dest)

        await asyncio.gather(*(fetch(handle) for handle in ready))
        return {handle: results[handle] for handle in handles}  # in the caller's order

    async def _collect(self, handle: JobHandle, dest: Path) -> OpOutcome[JobOutput]:
        """Copy what a finished job's specification asks for into ``dest``."""
        directory = self._jobs.copy_dir(handle)
        target = dest / quote(self._jobs.resource, safe="") / handle.id
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            manifest = await self._read_manifest(handle)
            listed = await self._jobs.run(collect.listing_command(directory))
            if manifest is None or listed.returncode != 0:
                return _not_collected("the job was purged; its output is gone")
            selection = collect.select(
                collect.parse_listing(listed.stdout),
                manifest,
                max_files=self._settings.max_output_files,
                max_bytes=self._settings.max_output_bytes,
            )
            async with asyncio.timeout(self._settings.transfer_timeout_s):
                return await self._copy(selection, directory, target)
        except (ValueError, OSError, ResourceUnavailableError) as exc:
            return _not_collected(str(exc))
        except TimeoutError:
            return _not_collected(
                f"collecting took longer than {self._settings.transfer_timeout_s} s"
            )

    async def _read_manifest(self, handle: JobHandle) -> staging.Manifest | None:
        submission = self._jobs.submission_dir(handle.routing["submission"])
        result = await self._jobs.run(
            ["cat", posixpath.join(submission, staging.MANIFEST)]
        )
        if result.returncode != 0:
            return None
        try:
            return staging.Manifest.model_validate_json(result.stdout)
        except ValidationError:
            raise ValueError("the job's manifest cannot be read") from None

    async def _copy(
        self, selection: collect.Selection, directory: str, target: Path
    ) -> OpOutcome[JobOutput]:
        """Copy into a new directory and put it in place only when all of it arrived."""
        partial = Path(tempfile.mkdtemp(prefix=".partial-", dir=target.parent))
        timeout = self._settings.transfer_timeout_s

        async def fetch(name: str, local: Path) -> Path:
            remote = posixpath.join(directory, name)
            await self._transport.get(remote, local, timeout=timeout)
            return target / local.relative_to(partial)

        try:
            streams: dict[str, Path] = {}
            for field in ("stdout", "stderr", "log"):
                if (name := getattr(selection, field)) is not None:
                    streams[field] = await fetch(name, partial / field)
            files = {}
            for name in selection.files:
                member = name.removeprefix(f"{layout.WORK}/")
                files[member] = await fetch(name, partial / "files" / member)
            shutil.rmtree(target, ignore_errors=True)  # a previous collection
            partial.rename(target)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
        return OpOutcome[JobOutput](ok=True, value=JobOutput(files=files, **streams))


def _not_collected(reason: str) -> OpOutcome[JobOutput]:
    return OpOutcome[JobOutput](ok=False, reason=reason)
