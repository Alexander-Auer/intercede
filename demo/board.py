"""The job board: what a consumer keeps, and the loop that polls it, for every column at once.

Every call into a backend goes through ``demo.consumer``, the code shown on
stage. The board only keeps what a consumer would keep: handles in its
database, and the last status it was told.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from demo import consumer
from demo.columns import Column, Columns
from demo.payloads import (
    PAYLOADS,
    FakeStep,
    Payload,
    copies,
    specification,
    write_scripts,
)
from intercede import InterCEdeError, JobHandle, JobStatus
from intercede._backends.fake import FakeBackend

POLL_INTERVAL_S = 2.0
FINISHED = {JobStatus.DONE.value, JobStatus.FAILED.value, JobStatus.ABORTED.value}
REFUSED = "Refused"  # a tile for a copy the backend refused: not a JobStatus


@dataclass
class Job:
    key: str
    column: str
    payload: str
    copy: int
    status: str
    handle: JobHandle | None = None
    reason: str = ""
    purged: bool = False
    history: list[tuple[float, str]] = field(default_factory=list)

    def set_status(self, status: str) -> bool:
        if status == self.status:
            return False
        self.status = status
        self.history.append((time.time(), status))
        return True

    def as_event(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "column": self.column,
            "payload": self.payload,
            "copy": self.copy,
            "status": self.status,
            "id": self.handle.id if self.handle else None,
            "handle": self.handle.model_dump(mode="json") if self.handle else None,
            "reason": self.reason,
            "purged": self.purged,
            "history": self.history,
        }


class Board:
    def __init__(self, columns: Columns, runtime: Path) -> None:
        self.columns = columns
        self.bus = columns.tracer.bus
        self.runtime = runtime
        self.scripts = write_scripts(runtime / "payloads")
        self.jobs: dict[str, Job] = {}
        self.database: dict[
            str, str
        ] = {}  # handle key -> stored JSON: the consumer's database
        self._drivers: set[asyncio.Task[None]] = set()
        self._dirty = False

    # -- what the browser sees ---------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "jobs": [job.as_event() for job in self.jobs.values()],
            "database": self.database,
        }

    def publish(self) -> None:
        self.bus.publish({"type": "board", **self.snapshot()})

    def alert(self, column: str, message: str, level: str = "error") -> None:
        self.bus.publish(
            {"type": "alert", "column": column, "level": level, "message": message}
        )

    # -- submitting ----------------------------------------------------------

    async def submit(
        self, column_ids: list[str], payload_name: str, count: int
    ) -> None:
        payload = PAYLOADS[payload_name]
        spec = specification(payload, self.scripts[payload_name])
        targets = [
            self.columns.columns[c]
            for c in column_ids
            if self.columns.columns[c].state == "online"
        ]
        await asyncio.gather(
            *(self._submit(column, payload, spec, count) for column in targets)
        )

    async def _submit(
        self, column: Column, payload: Payload, spec: Any, count: int
    ) -> None:
        per_copy = copies(count)
        try:
            submission = await consumer.submit(
                column.backend, spec, per_copy, self.database
            )
        except InterCEdeError as exc:
            self.alert(column.id, f"submit raised {type(exc).__name__}: {exc}")
            return
        for index, handle in submission.handles.items():
            job = Job(
                handle.key,
                column.id,
                payload.name,
                index,
                JobStatus.SUBMITTED.value,
                handle,
            )
            job.history.append((time.time(), job.status))
            self.jobs[job.key] = job
            if column.fake is not None:
                self._drive(column.fake, handle, payload, index)
        for index, reason in submission.failures.items():
            key = f"{column.id}:refused:{time.time_ns()}:{index}"
            self.jobs[key] = Job(
                key, column.id, payload.name, index, REFUSED, reason=reason
            )
        self.publish()

    # -- the Fake: a resource on a timer --------------------------------------

    def _drive(
        self, fake: FakeBackend, handle: JobHandle, payload: Payload, copy: int
    ) -> None:
        task = asyncio.create_task(_play(fake, handle, payload.fake, copy))
        self._drivers.add(task)
        task.add_done_callback(self._drivers.discard)

    # -- polling, as a consumer does -----------------------------------------

    async def poll_forever(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            await self.poll_once()

    async def poll_once(self, only_finished_too: bool = False) -> None:
        for column in list(self.columns.columns.values()):
            if column.state != "online":
                continue
            jobs = [
                job
                for job in self.jobs.values()
                if job.column == column.id
                and job.handle is not None
                and (only_finished_too or job.status not in FINISHED)
            ]
            if not jobs:
                continue
            try:
                statuses = await consumer.poll(
                    column.backend, [job.handle for job in jobs if job.handle]
                )
            except InterCEdeError as exc:
                self.alert(column.id, f"get_status raised {type(exc).__name__}: {exc}")
                continue
            except (
                Exception
            ) as exc:  # e.g. the backend was closed by a restart mid-poll
                self.alert(column.id, f"get_status failed: {exc}", "warning")
                continue
            changed = False
            for job in jobs:
                assert job.handle is not None
                changed |= job.set_status(str(statuses[job.handle]))
            if changed:
                self.publish()

    # -- the optional capabilities -------------------------------------------

    async def act(self, operation: str, keys: list[str]) -> dict[str, Any]:
        """Run one capability on the given jobs, one call per column, as a consumer would."""
        results: dict[str, Any] = {}
        by_column: dict[str, list[Job]] = {}
        for key in keys:
            job = self.jobs.get(key)
            if job is not None and job.handle is not None:
                by_column.setdefault(job.column, []).append(job)
        for column_id, jobs in by_column.items():
            column = self.columns.columns[column_id]
            if column.state != "online":
                results.update(
                    {
                        j.key: {"ok": False, "reason": "the backend is offline"}
                        for j in jobs
                    }
                )
                continue
            dest = self.runtime / "output" / column_id
            dest.mkdir(parents=True, exist_ok=True)
            try:
                outcomes = await consumer.act(
                    column.backend,
                    operation,
                    [j.handle for j in jobs if j.handle],
                    dest,
                )
            except NotImplementedError as exc:
                results.update(
                    {
                        j.key: {"ok": False, "reason": str(exc), "unsupported": True}
                        for j in jobs
                    }
                )
                continue
            except InterCEdeError as exc:
                results.update(
                    {
                        j.key: {
                            "ok": False,
                            "reason": f"{type(exc).__name__}: {exc}",
                            "raised": True,
                        }
                        for j in jobs
                    }
                )
                continue
            for job in jobs:
                outcome = outcomes[job.handle]
                results[job.key] = {
                    "ok": outcome.ok,
                    "reason": outcome.reason,
                    "value": self._shown(outcome.value),
                }
                if operation == "purge" and outcome.ok:
                    job.purged = True
        self.bus.publish(
            {"type": "outcomes", "operation": operation, "results": results}
        )
        await self.poll_once(only_finished_too=True)
        self.publish()
        return results

    def _shown(self, value: Any) -> Any:
        """A JobOutput's paths as paths the browser may open; anything else as data."""
        if value is None:
            return None
        data = value.model_dump(mode="json")
        if "files" in data:  # a JobOutput: paths relative to the runtime directory

            def rel(path: str | None) -> str | None:
                return str(Path(path).relative_to(self.runtime)) if path else None

            data = {
                "stdout": rel(data["stdout"]),
                "stderr": rel(data["stderr"]),
                "log": rel(data["log"]),
                "files": {name: rel(path) for name, path in data["files"].items()},
            }
        return data

    # -- reset -------------------------------------------------------------------

    async def reset(self) -> None:
        """Release every job that can be released, then forget everything."""
        for task in list(self._drivers):
            task.cancel()
        purgeable = [
            job.key
            for job in self.jobs.values()
            if job.handle is not None and not job.purged
        ]
        with contextlib.suppress(Exception):
            if purgeable:
                await self.act("purge", purgeable)
        self.jobs.clear()
        self.database.clear()
        shutil.rmtree(self.runtime / "output", ignore_errors=True)
        self.columns.tracer.clear()
        self.bus.publish({"type": "reset"})
        self.publish()


async def _play(
    fake: FakeBackend, handle: JobHandle, steps: tuple[FakeStep, ...], copy: int
) -> None:
    """Move one fake job through its payload's timeline, with the fake's ``finish`` hook.

    Stops as soon as the job is no longer where the timeline left it: killed,
    purged, or finished.
    """
    started = time.monotonic()
    for step in steps:
        await asyncio.sleep(max(0.0, started + step.after - time.monotonic()))
        try:
            current = (await fake.get_status([handle]))[handle]
        except InterCEdeError:  # closed for a moment by a restart: the job goes on
            continue
        if current in (
            JobStatus.ABORTED,
            JobStatus.UNKNOWN,
            JobStatus.DONE,
            JobStatus.FAILED,
        ):
            return
        with contextlib.suppress(KeyError):  # purged between the check and the hook
            fake.finish(
                handle,
                status=JobStatus(step.status),
                stdout=step.stdout.replace("$COPY_INDEX", str(copy)),
                files={
                    name: content.replace(b"$COPY_INDEX", str(copy).encode())
                    for name, content in step.files.items()
                },
            )
