"""Killing and purging jobs on an ARC CE: A-REX's ``kill`` and ``clean``.

A-REX accepts a kill of a job that has ended too, and does nothing with it, so
only the state tells a finished job from a cancelled one. ``clean`` removes a
job and its files, after which A-REX no longer knows it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from intercede._backends._arc import rest
from intercede._backends.common import ACTIVE
from intercede._backends.credentials import CANCEL_SCOPES
from intercede._backends.monolith.arc.jobs import Jobs
from intercede.models import JobHandle, OpOutcome

_UNKNOWN_JOB = OpOutcome[None](ok=False, reason="unknown job")
_NOTHING_LEFT = OpOutcome[None](
    ok=True, reason="the CE no longer knows the job; nothing is left to release"
)


class JobCanceller:
    """Kills and purges jobs on the CE."""

    def __init__(self, jobs: Jobs) -> None:
        self._jobs = jobs

    async def kill(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        """Cancel each active job; see ``Killable.kill``."""
        # A-REX accepts a kill for a job that has ended too, and does nothing
        # with it: only the state tells a finished job from a cancelled one.
        states = await self._jobs.states(self._jobs.ids(handles))
        active = [job for job, state in states.items() if state in ACTIVE]
        killed = await self._jobs.ask("kill", active, CANCEL_SCOPES)
        outcomes: dict[JobHandle, OpOutcome[None]] = {}
        for handle in handles:
            if not self._jobs.owns(handle) or handle.id not in states:
                outcomes[handle] = _UNKNOWN_JOB
            elif handle.id in active:
                outcomes[handle] = _done(killed.get(handle.id))
            else:
                outcomes[handle] = OpOutcome[None](ok=True, reason="already finished")
        return outcomes

    async def purge(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        """Cancel each job and remove it from the CE; see ``Purgeable.purge``."""
        states = await self._jobs.states(self._jobs.ids(handles))
        active = [job for job, state in states.items() if state in ACTIVE]
        killed = await self._jobs.ask("kill", active, CANCEL_SCOPES)
        # A-REX keeps a clean of a job that is still active until it has ended,
        # so a job cancelled here goes once it has stopped. One that could not
        # be cancelled keeps its files.
        refused = {job for job in active if not _done(killed.get(job)).ok}
        cleaned = await self._jobs.ask(
            "clean", [job for job in states if job not in refused], CANCEL_SCOPES
        )
        outcomes: dict[JobHandle, OpOutcome[None]] = {}
        for handle in handles:
            if not self._jobs.owns(handle):
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
