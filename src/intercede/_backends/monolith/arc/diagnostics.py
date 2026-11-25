"""Reading a running job's files: the end of one file in its session directory.

The session directory is the job's working directory, and A-REX serves it while
the job runs. A running job writes at the end of a file, so the last MiB is read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from intercede._backends._arc import rest
from intercede._backends._arc.description import read_kept
from intercede._backends.common import in_parallel
from intercede._backends.credentials import READ_SCOPES
from intercede._backends.monolith.arc.connection import PARALLEL_TRANSFERS, Connection
from intercede._backends.monolith.arc.jobs import Jobs
from intercede.exceptions import ResourceUnavailableError
from intercede.models import Diagnostics, JobHandle, OpOutcome

# How much of a file get_diagnostics() reads: its end, where a running job writes.
DIAGNOSTICS_BYTES = 2**20


class DiagnosticsReader:
    """Reads the end of a file of each job, while it runs or after."""

    def __init__(self, jobs: Jobs, connection: Connection) -> None:
        self._jobs = jobs
        self._connection = connection

    async def get_diagnostics(
        self, handles: Sequence[JobHandle], filename: str | None = None
    ) -> Mapping[JobHandle, OpOutcome[Diagnostics]]:
        """Read the end of a file in each job's session directory, the last MiB.

        The session directory is the job's working directory, and the CE serves
        it while the job runs. ``None`` reads the job's standard output.
        """
        states = await self._jobs.states(self._jobs.ids(handles))
        results: dict[JobHandle, OpOutcome[Diagnostics]] = {}
        known: dict[str, list[JobHandle]] = {}
        for handle in handles:
            if self._jobs.owns(handle) and handle.id in states:
                known.setdefault(handle.id, []).append(handle)
            else:
                results[handle] = _not_read("unknown job")

        read = await in_parallel(
            (self._diagnose(job_id, filename) for job_id in known), PARALLEL_TRANSFERS
        )
        for same, outcome in zip(known.values(), read):
            results.update(dict.fromkeys(same, outcome))
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


def _not_read(reason: str) -> OpOutcome[Diagnostics]:
    return OpOutcome[Diagnostics](ok=False, reason=reason)
