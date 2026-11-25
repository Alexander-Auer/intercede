"""Collecting a finished job's output from its session directory on the CE.

The backend keeps no record of a job: what it keeps for collection is read back
from the description A-REX kept of it. ``collect`` chooses what to copy from a
listing of the session directory, before a single file is copied.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

from intercede._backends._arc import rest
from intercede._backends._arc.description import read_kept
from intercede._backends.common import ACTIVE, in_parallel
from intercede._backends.credentials import READ_SCOPES
from intercede._backends.monolith.arc import collect
from intercede._backends.monolith.arc.connection import PARALLEL_TRANSFERS, Connection
from intercede._backends.monolith.arc.jobs import Jobs
from intercede.config import ARCConfig
from intercede.exceptions import ResourceUnavailableError
from intercede.models import JobHandle, JobOutput, OpOutcome


class OutputCollector:
    """Copies the output of finished jobs into a directory of the caller's."""

    def __init__(self, settings: ARCConfig, jobs: Jobs, connection: Connection) -> None:
        self._settings = settings
        self._jobs = jobs
        self._connection = connection

    async def get_output(
        self, handles: Sequence[JobHandle], dest: Path
    ) -> Mapping[JobHandle, OpOutcome[JobOutput]]:
        """Collect each finished job's output; see ``OutputRetriever.get_output``."""
        states = await self._jobs.states(self._jobs.ids(handles))
        results: dict[JobHandle, OpOutcome[JobOutput]] = {}
        ready: dict[str, list[JobHandle]] = {}  # each job once
        for handle in handles:
            state = states.get(handle.id) if self._jobs.owns(handle) else None
            if state is None:
                results[handle] = _not_collected("unknown job")
            elif state in ACTIVE:
                results[handle] = _not_collected("job has not finished")
            else:
                ready.setdefault(handle.id, []).append(handle)

        collected = await in_parallel(
            (self._collect(job_id, dest) for job_id in ready), PARALLEL_TRANSFERS
        )
        for same, outcome in zip(ready.values(), collected):
            results.update(dict.fromkeys(same, outcome))
        return {handle: results[handle] for handle in handles}  # in the caller's order

    async def _collect(self, job_id: str, dest: Path) -> OpOutcome[JobOutput]:
        """Copy what a finished job's specification keeps into ``dest``."""
        target = dest / quote(self._jobs.resource, safe="") / job_id
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


def _not_collected(reason: str) -> OpOutcome[JobOutput]:
    return OpOutcome[JobOutput](ok=False, reason=reason)
