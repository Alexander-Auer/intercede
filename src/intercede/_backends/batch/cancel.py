"""Killing and purging jobs: the scheduler's cancel command, then removing their files.

``kill`` cancels only active jobs. ``purge`` cancels them as well, then removes
each job's directory, and its submission's once no copy is left in it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from intercede._backends.batch import layout
from intercede._backends.batch.jobs import ACTIVE, CHUNK, Jobs, chunks
from intercede._backends.batch.protocols import CommandResult, Scheduler
from intercede.models import JobHandle, JobID, OpOutcome


class JobCanceller:
    """Kills and purges jobs on the resource."""

    def __init__(self, jobs: Jobs, scheduler: Scheduler) -> None:
        self._jobs = jobs
        self._scheduler = scheduler

    async def kill(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        """Cancel each active job; see ``Killable.kill``."""
        ids = self._jobs.ids(handles)
        states = await self._jobs.states(ids)
        # Only active jobs: a finished one has nothing left to cancel.
        refused = await self._cancel([i for i in ids if states.get(i) in ACTIVE])
        results = {}
        for handle in handles:
            state = states.get(handle.id) if self._jobs.owns(handle) else None
            if state is None:
                results[handle] = OpOutcome[None](ok=False, reason="unknown job")
            elif handle.id in refused:
                results[handle] = OpOutcome[None](ok=False, reason=refused[handle.id])
            elif state in ACTIVE:
                results[handle] = OpOutcome[None](ok=True)
            else:
                results[handle] = OpOutcome[None](ok=True, reason="already finished")
        return results

    async def purge(
        self, handles: Sequence[JobHandle]
    ) -> Mapping[JobHandle, OpOutcome[None]]:
        """Cancel each job and remove its files; see ``Purgeable.purge``."""
        ids = self._jobs.ids(handles)
        states = await self._jobs.states(ids)
        refused = await self._cancel([i for i in ids if states.get(i) in ACTIVE])
        ours = [h for h in handles if self._jobs.owns(h) and h.id not in refused]
        failed = await self._remove(ours)
        results = {}
        for handle in handles:
            if not self._jobs.owns(handle):
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
        for chunk in chunks(ids):
            result = await self._jobs.run(self._scheduler.kill_command(chunk))
            if result.returncode != 0:
                failures.update(dict.fromkeys(chunk, _reason(result)))
        if not failures:
            return {}
        # A tool does not say which ID failed, so see what really happened: a
        # job may also have ended in the meantime.
        states = await self._jobs.states(list(failures))
        return {i: why for i, why in failures.items() if states.get(i) in ACTIVE}

    async def _remove(self, handles: Sequence[JobHandle]) -> dict[JobHandle, str]:
        """Remove jobs' files; return the reason for each whose files could not be."""
        failed: dict[JobHandle, str] = {}
        unique = list(dict.fromkeys(handles))
        for start in range(0, len(unique), CHUNK):
            chunk = unique[start : start + CHUNK]
            result = await self._jobs.run(
                ["rm", "-rf", *map(self._jobs.copy_dir, chunk)]
            )
            if result.returncode != 0:
                failed.update(dict.fromkeys(chunk, _reason(result)))
        submissions = {h.routing["submission"] for h in unique if h not in failed}
        for submission in submissions:
            workdir = self._jobs.submission_dir(submission)
            # Fails while other copies remain, which then still need the inputs.
            emptied = await self._jobs.run(["rmdir", f"{workdir}/{layout.COPIES}"])
            if emptied.returncode == 0:
                await self._jobs.run(["rm", "-rf", workdir])
        return failed


def _reason(result: CommandResult) -> str:
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else f"exit code {result.returncode}"
