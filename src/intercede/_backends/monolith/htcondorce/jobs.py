"""The CE's jobs as every operation sees them: which are ours, and what the CE knows.

Handles come from the consumer's storage, so ``owns`` checks one before its id
reaches a command line. Every request runs in a ``session``, with a token
scoped to its operation.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from intercede._backends._htcondor import status
from intercede._backends.common import owned_ids, statuses
from intercede._backends.credentials import (
    READ_SCOPES,
    CredentialCache,
    credential_requirements,
)
from intercede._backends.monolith.htcondorce import staging
from intercede._backends.monolith.htcondorce.process import Runner
from intercede._backends.monolith.htcondorce.session import (
    CondorSession,
    credential_files,
)
from intercede.auth.models import BearerToken
from intercede.config import HTCondorCEConfig
from intercede.models import JobHandle, JobID, StatusMap

BACKEND = "htcondor-ce"
DEFAULT_PORT = 9619

_JOB_ID = re.compile(r"^\d+\.\d+$")


class Jobs:
    """The jobs of one CE, and the sessions every operation asks it through."""

    def __init__(
        self,
        settings: HTCondorCEConfig,
        credentials: CredentialCache,
        runner: Runner,
        workdir: Path,
    ) -> None:
        self.resource = settings.endpoint
        self.workdir = workdir
        self.host, port = split_endpoint(settings.endpoint)
        self._settings = settings
        self._credentials = credentials
        self._run = runner
        self._pool = f"{self.host}:{port}"
        # The CE's schedd is named after its host unless the site says otherwise.
        self._name = settings.schedd or self.host

    def owns(self, handle: JobHandle) -> bool:
        """Tell whether a stored handle is one of this backend's, and well-formed."""
        return (
            handle.backend == BACKEND
            and handle.resource == self.resource
            and bool(_JOB_ID.match(handle.id))
        )

    def ids(self, handles: Iterable[JobHandle]) -> list[JobID]:
        """Return the ids to ask the CE about: ours, well-formed, each once.

        Ids come from stored handles, so they are checked before they reach a
        command line: a malformed one could otherwise pass as an option.
        """
        return owned_ids(handles, self.owns)

    async def status(self, handles: Sequence[JobHandle]) -> StatusMap:
        """Return the status of each job, in the caller's order."""
        ads = await self.ads(self.ids(handles), status.STATE_ATTRIBUTES, history=True)
        states = {job: status.job_status(ad) for job, ad in ads.items()}
        return statuses(handles, self.owns, states)

    async def ads(
        self, ids: Sequence[JobID], attributes: Sequence[str], *, history: bool
    ) -> dict[JobID, Mapping[str, Any]]:
        """Return the job ad of each job the CE knows, with ``attributes``."""
        if not ids:
            return {}
        async with self.session(READ_SCOPES) as session:
            return await session.ads(ids, attributes, history=history)

    @asynccontextmanager
    async def session(self, scopes: frozenset[str]) -> AsyncGenerator[CondorSession]:
        """Open the tools for one operation, with a token scoped to it."""
        token = await self._fresh(scopes)
        workdir = staging.ensure_workdir(self.workdir)
        async with credential_files(token, workdir) as files:
            yield CondorSession(self._run, files, self._pool, self._name)

    async def _fresh(self, scopes: frozenset[str]) -> BearerToken:
        requirements = credential_requirements(self._settings, scopes)
        assert requirements is not None  # an HTCondor-CE always asks for a token
        token = await self._credentials.fresh(requirements)
        assert isinstance(token, BearerToken)  # the cache checked the kind
        return token


def split_endpoint(endpoint: str) -> tuple[str, int]:
    """Split ``host[:port]``; an HTCondor-CE listens on 9619 by default."""
    host, separator, port = endpoint.partition(":")
    return host, int(port) if separator else DEFAULT_PORT
