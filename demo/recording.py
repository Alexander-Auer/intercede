"""Recording a live session, and playing it back through the same views.

A recording is the stream of events the browser saw, which the bus has already
redacted, preceded by a full snapshot. Playing it back sends the same events
again, so the views draw exactly what they drew live; the badge says REPLAY.
While a recording plays, live events reach no browser.

The background polls (``get_status`` on a timer) are left out: they are most of
the events, and the board's own updates already carry what they found.
Recordings are gzipped JSON, to keep them small enough to commit.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from demo.events import Bus

RECORDINGS = Path(__file__).parent / "recordings"
FORMAT = 1


class Recorder:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.events: list[dict[str, Any]] = []
        self._started = 0.0
        self._untap: Callable[[], None] | None = None
        self._polls: set[int] = set()  # operation ids of the polls left out

    @property
    def on(self) -> bool:
        return self._untap is not None

    def start(self, snapshot: dict[str, Any]) -> None:
        self._started = time.time()
        self._polls.clear()
        self.events = [{"dt": 0.0, "event": self.bus.redactor.scrub(snapshot)}]
        self._untap = self.bus.tap(self._keep)

    def _keep(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "op.start" and event["op"]["name"] == "get_status":
            self._polls.add(event["op"]["id"])
            return
        if kind in ("op.step", "op.end") and event["id"] in self._polls:
            return
        self.events.append(
            {"dt": round(time.time() - self._started, 3), "event": event}
        )

    def stop(self) -> Path:
        if self._untap is not None:
            self._untap()
            self._untap = None
        RECORDINGS.mkdir(exist_ok=True)
        path = RECORDINGS / f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.json.gz"  # noqa: DTZ005  (a local file name)
        data = {
            "format": FORMAT,
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration_s": self.events[-1]["dt"] if self.events else 0,
            "events": self.events,
        }
        path.write_bytes(gzip.compress(json.dumps(data).encode()))
        return path


def recordings() -> list[dict[str, Any]]:
    found = []
    for path in sorted(RECORDINGS.glob("*.json.gz"), reverse=True):
        data = load(path)
        found.append(
            {
                "name": path.name,
                "recorded_at": data["recorded_at"],
                "duration_s": data["duration_s"],
                "events": len(data["events"]),
            }
        )
    return found


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(gzip.decompress(path.read_bytes()))
    if data.get("format") != FORMAT:
        raise ValueError(f"{path.name} is not a recording this demo can play")
    return data


class Player:
    """Plays a recording to every browser, at an adjustable speed."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.name: str | None = None
        self.played: list[
            dict[str, Any]
        ] = []  # sent so far, for a browser that connects meanwhile
        self.speed = 1.0
        self.paused = False
        self.position = 0.0
        self.duration = 0.0
        self._task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.name is not None

    def status(self) -> dict[str, Any]:
        return {
            "type": "replay",
            "name": self.name,
            "speed": self.speed,
            "paused": self.paused,
            "position": round(self.position, 1),
            "duration": self.duration,
            "done": self._task is None or self._task.done(),
        }

    def play(self, name: str) -> None:
        self.stop()
        data = load(RECORDINGS / Path(name).name)
        self.name, self.played, self.paused, self.position = name, [], False, 0.0
        self.duration = data["duration_s"]
        self.bus.live = False
        self._task = asyncio.create_task(self._run(data["events"]))

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.name = None
        self.played = []

    async def _run(self, events: list[dict[str, Any]]) -> None:
        clock = 0.0
        reported = time.monotonic()
        for item in events:
            while item["dt"] > clock:
                await asyncio.sleep(0.05)
                if not self.paused:
                    clock += 0.05 * self.speed
                    self.position = min(clock, item["dt"])
                if time.monotonic() - reported > 1:  # progress, for the replay bar
                    reported = time.monotonic()
                    self.bus.publish(self.status(), replayed=True)
            event = {**item["event"], "replayed": True}
            self.played.append(event)
            self.bus.publish(event, replayed=True)
        self.position = self.duration
        self.bus.publish(self.status(), replayed=True)
