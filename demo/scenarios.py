"""Scripted demonstrations: interCEde keeps no state, and reports partial failure per job.

Each runs through the same consumer code as the board, on the board's own jobs,
and publishes its progress as it goes, so a recording replays it as it happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any

from demo import consumer
from demo.board import FINISHED, Board
from intercede import InterCEdeError, JobHandle, JobStatus

WAIT_LIMIT_S = 150.0


class Scenarios:
    def __init__(self, board: Board) -> None:
        self.board = board
        self.bus = board.bus
        self.running = False
        # The latest of each, for a browser that connects afterwards.
        self.last: dict[str, dict[str, Any]] = {}

    # -- scene 6: the stateless handle -------------------------------------------

    async def restart(self, column_id: str) -> dict[str, Any]:
        """Close a backend, build a new one from configuration, and ask about the stored handles.

        The handles come from the consumer's database, as JSON, not from the
        board's memory: they are read back with ``JobHandle.model_validate_json``.
        """
        column = self.board.columns.columns[column_id]
        before = id(column.backend.inner) if column.backend else None
        steps: list[dict[str, Any]] = []

        def note(text: str, **extra: Any) -> None:
            steps.append({"t": time.time(), "text": text, **extra})
            self._publish({"type": "restart", "column": column_id, "steps": steps})

        note(f"backend {_hex(before)}: __aexit__ (closed)")
        await self.board.columns.rebuild(column)
        if column.backend is None:
            note(f"rebuilding failed: {column.reason}", error=True)
            return {"steps": steps}
        if column.kind == "fake":
            # The Fake is its own resource: reopening the same object keeps its "queue".
            note(
                f"backend {_hex(id(column.backend.inner))}: the Fake reopened (it is its own resource)"
            )
        else:
            note(
                f"backend {_hex(id(column.backend.inner))}: a new one, built from configuration, opened"
            )
        # Which stored handles are this backend's: each handle says where its job is.
        stored = [
            handle
            for handle in map(
                JobHandle.model_validate_json, self.board.database.values()
            )
            if handle.resource == column.backend.resource
        ]
        note(f"{len(stored)} handles read back from the consumer's database")
        if stored:
            statuses = await consumer.poll(column.backend, stored)
            for handle in stored:
                job = self.board.jobs.get(handle.key)
                if job is not None:
                    job.set_status(str(statuses[handle]))
            note(
                "get_status on the new backend",
                statuses={h.id: str(statuses[h]) for h in stored},
            )
            self.board.publish()
        return {"steps": steps}

    async def forge(self, key: str) -> dict[str, Any]:
        """Ask about a handle like a stored one, but with an ID the resource never gave out."""
        job = self.board.jobs[key]
        assert job.handle is not None
        column = self.board.columns.columns[job.column]
        forged = job.handle.model_copy(
            update={"id": re.sub(r"\d+", "999999", job.handle.id)}
        )
        statuses = await consumer.poll(column.backend, [forged])
        result = {
            "column": job.column,
            "handle": forged.model_dump(mode="json"),
            "status": str(statuses[forged]),
        }
        self._publish({"type": "forged", **result})
        return result

    # -- scene 7: partial failure -------------------------------------------------

    async def run(self, name: str, column_id: str) -> None:
        if self.running:
            raise RuntimeError("a scenario is already running")
        self.running = True
        scenario = {
            "type": "scenario",
            "name": name,
            "column": column_id,
            "steps": [],
            "done": False,
        }
        try:
            await getattr(self, f"_{name}")(column_id, scenario)
        except Exception as exc:  # shown in the scene, then the demo goes on
            self._step(scenario, f"stopped: {type(exc).__name__}: {exc}", error=True)
        finally:
            self.running = False
            scenario["done"] = True
            self._publish(scenario)

    def _step(self, scenario: dict[str, Any], title: str, **detail: Any) -> None:
        scenario["steps"].append({"title": title, **detail})
        self._publish(scenario)

    def _publish(self, event: dict[str, Any]) -> None:
        self.last[event["type"]] = event
        self.bus.publish(event)

    def _fake(self, column_id: str) -> Any:
        fake = self.board.columns.columns[column_id].fake
        if fake is None:
            raise RuntimeError("this scenario uses the Fake backend's test hooks")
        return fake

    async def _refused_copies(self, column_id: str, scenario: dict[str, Any]) -> None:
        fake = self._fake(column_id)
        fake.refuse_copies = {1, 3}
        self._step(
            scenario,
            "The resource will refuse copies 1 and 3 (FakeBackend(refuse_copies={1, 3}))",
        )
        try:
            submitted = (await self.board.submit([column_id], "quick", 5))[column_id]
        finally:
            fake.refuse_copies = set()
        self._step(
            scenario,
            "submit(spec, 5 copies) returns, without raising",
            submission={
                "handles": {str(i): h.id for i, h in submitted.handles.items()},
                "failures": {str(i): r for i, r in submitted.failures.items()},
            },
        )

    async def _injected_failures(
        self, column_id: str, scenario: dict[str, Any]
    ) -> None:
        fake = self._fake(column_id)
        submitted = (await self.board.submit([column_id], "progress", 4))[column_id]
        handles = list(submitted.handles.values())
        broken = {handles[1].id, handles[2].id}
        fake.fail_jobs = broken
        self._step(
            scenario,
            f"Jobs {sorted(broken)} now fail on every operation (FakeBackend(fail_jobs=...))",
        )
        try:
            outcomes = await self.board.act("kill", [h.key for h in handles])
        finally:
            fake.fail_jobs = set()
        self._step(
            scenario,
            "kill(4 handles): one outcome per job",
            outcomes=_short(outcomes, self.board),
        )

    async def _unavailable(self, column_id: str, scenario: dict[str, Any]) -> None:
        fake = self._fake(column_id)
        backend = self.board.columns.columns[column_id].backend
        handles = [
            j.handle
            for j in self.board.jobs.values()
            if j.column == column_id and j.handle
        ]
        if not handles:
            handles = list(
                (await self.board.submit([column_id], "quick", 2))[
                    column_id
                ].handles.values()
            )
        fake.unavailable = True
        self._step(
            scenario, "The whole resource goes down (FakeBackend.unavailable = True)"
        )
        try:
            await consumer.poll(backend, handles)
        except InterCEdeError as exc:
            self._step(
                scenario,
                "get_status raises: the whole operation failed",
                raised=f"{type(exc).__name__}: {exc}",
            )
        finally:
            fake.unavailable = False
        statuses = await consumer.poll(backend, handles)
        self._step(
            scenario,
            "Back up: the same call answers per job",
            statuses={h.id: str(statuses[h]) for h in handles},
        )

    async def _kill_mix(self, column_id: str, scenario: dict[str, Any]) -> None:
        self._step(scenario, "Submit 2 quick jobs and 2 jobs of 30 s")
        quick = (await self.board.submit([column_id], "quick", 2))[column_id]
        slow = (await self.board.submit([column_id], "progress", 2))[column_id]
        if isinstance(quick, Exception) or isinstance(slow, Exception):
            raise RuntimeError("submitting failed; see the board")
        keys = [h.key for h in (*quick.handles.values(), *slow.handles.values())]
        await self._wait(
            lambda: all(self._status(h.key) in FINISHED for h in quick.handles.values())
            and any(
                self._status(h.key) == JobStatus.RUNNING.value
                for h in slow.handles.values()
            )
        )
        self._step(
            scenario,
            "Now: the quick ones finished, the long ones run",
            statuses={self.board.jobs[k].id_: self._status(k) for k in keys},
        )
        outcomes = await self.board.act("kill", keys)
        self._step(
            scenario,
            "kill(all 4): cancelled where active, left alone where finished",
            outcomes=_short(outcomes, self.board),
        )

    async def _output_too_early(self, column_id: str, scenario: dict[str, Any]) -> None:
        slow = (await self.board.submit([column_id], "progress", 1))[column_id]
        if isinstance(slow, Exception):
            raise RuntimeError("submitting failed; see the board")
        keys = [h.key for h in slow.handles.values()]
        outcomes = await self.board.act("get_output", keys)
        self._step(
            scenario,
            "get_output right after submit: refused for that job, nothing raised",
            outcomes=_short(outcomes, self.board),
        )
        await self.board.act("kill", keys)

    async def _purge_then_output(
        self, column_id: str, scenario: dict[str, Any]
    ) -> None:
        quick = (await self.board.submit([column_id], "files", 2))[column_id]
        if isinstance(quick, Exception):
            raise RuntimeError("submitting failed; see the board")
        keys = [h.key for h in quick.handles.values()]
        await self._wait(lambda: all(self._status(k) in FINISHED for k in keys))
        first = await self.board.act("get_output", keys)
        self._step(
            scenario, "get_output: collected", outcomes=_short(first, self.board)
        )
        again = await self.board.act("get_output", keys)
        self._step(
            scenario,
            "get_output again: collection is repeatable",
            outcomes=_short(again, self.board),
        )
        purged = await self.board.act("purge", keys[:1])
        self._step(
            scenario, "purge(the first job)", outcomes=_short(purged, self.board)
        )
        after = await self.board.act("get_output", keys)
        self._step(
            scenario,
            "get_output on both: per job, the purged one is gone",
            outcomes=_short(after, self.board),
        )

    def _status(self, key: str) -> str:
        job = self.board.jobs.get(key)
        return job.status if job else "?"

    async def _wait(self, ready: Any) -> None:
        deadline = time.monotonic() + WAIT_LIMIT_S
        while not ready():
            if time.monotonic() > deadline:
                raise TimeoutError("the jobs did not get there in time")
            await asyncio.sleep(1)
            with contextlib.suppress(Exception):
                await self.board.poll_once()


SCENARIOS = {
    "refused_copies": ("Some copies refused", "fake"),
    "injected_failures": ("Per-job failures", "fake"),
    "unavailable": ("Whole resource down", "fake"),
    "kill_mix": ("Kill running and finished jobs", "any"),
    "output_too_early": ("Output before the job finished", "any"),
    "purge_then_output": ("Collect, collect again, purge, collect", "any"),
}


def _short(outcomes: dict[str, Any], board: Board) -> dict[str, Any]:
    shown = {}
    for key, outcome in outcomes.items():
        job = board.jobs.get(key)
        shown[job.handle.id if job and job.handle else key] = {
            "ok": outcome["ok"],
            "reason": outcome.get("reason"),
        }
    return shown


def _hex(value: int | None) -> str:
    return f"0x{value:x}" if value is not None else "none"
