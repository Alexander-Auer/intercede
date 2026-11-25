"""The CE's jobs as every operation sees them: which are ours, and their states.

Handles come from the consumer's storage, so ``owns`` checks one before its id
reaches a URL. A-REX answers a request about several jobs per job, so ``ask``
returns its answers by job.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from intercede._backends._arc import rest, status
from intercede._backends.common import owned_ids, statuses
from intercede._backends.credentials import READ_SCOPES
from intercede._backends.monolith.arc.connection import Connection
from intercede.models import JobHandle, JobID, JobStatus, StatusMap

BACKEND = "arc"


class Jobs:
    """The jobs of one CE, and the requests every operation makes about them."""

    def __init__(self, resource: str, connection: Connection) -> None:
        self.resource = resource
        self._connection = connection

    def owns(self, handle: JobHandle) -> bool:
        """Tell whether a stored handle is one of this backend's, and well-formed."""
        return (
            handle.backend == BACKEND
            and handle.resource == self.resource
            and rest.is_job_id(handle.id)
        )

    def ids(self, handles: Iterable[JobHandle]) -> list[JobID]:
        """Return the ids to ask the CE about: ours, well-formed, each once."""
        return owned_ids(handles, self.owns)

    async def status(self, handles: Sequence[JobHandle]) -> StatusMap:
        """Return the status of each job, in the caller's order."""
        return statuses(handles, self.owns, await self.states(self.ids(handles)))

    async def states(self, ids: Sequence[str]) -> dict[JobID, JobStatus]:
        """Return the state of each job the CE knows; it answers 404 for the others."""
        answers = await self.ask("status", ids, READ_SCOPES)
        return {
            JobID(job): status.job_status(answer.state)
            for job, answer in answers.items()
            if answer.ok and answer.state is not None
        }

    async def ask(
        self, action: str, ids: Sequence[str], scopes: frozenset[str]
    ) -> dict[str, rest.JobAnswer]:
        """Ask the CE to do ``action`` for each job of ``ids``; its answers by job."""
        if not ids:
            return {}
        answers = await self._connection.jobs(action, ids, scopes)
        return {answer.id: answer for answer in answers if answer.id is not None}
