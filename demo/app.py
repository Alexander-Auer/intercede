"""The presentation server: one event stream to the browser, and the actions a presenter takes."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from demo import consumer, doors, dryrun, views
from demo.board import Board
from demo.columns import Columns, compose_file, stacks
from demo.events import Bus
from demo.payloads import PAYLOADS
from demo.recording import Player, Recorder, recordings
from demo.scenarios import SCENARIOS, Scenarios
from demo.scripts import fetch_ci
from demo.tracing import Tracer
from intercede import JobStatus

HERE = Path(__file__).parent
STATIC = HERE / "static"
RUNTIME = HERE / ".runtime"  # payload scripts and collected output; git-ignored
STACK_REFRESH_S = 5.0
HEARTBEAT_S = 15.0
CI_RUNS = 12  # runs per repository on the CI scene


class State:
    """Everything the server holds; built at start-up, closed at shutdown."""

    def __init__(self) -> None:
        self.bus = Bus()
        self.tracer = Tracer(self.bus)
        self.columns = Columns(self.tracer)
        self.board = Board(self.columns, RUNTIME)
        self.scenarios = Scenarios(self.board)
        self.mode = "live"
        self.recorder = Recorder(self.bus)
        self.player = Player(self.bus)
        self.tasks: list[asyncio.Task[None]] = []

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "mode": self.mode,
            "columns": self.columns.snapshot(),
            "operations": [op.as_event() for op in self.tracer.operations],
            "last": dict(self.scenarios.last),
            "recording": self.recorder.on,
            **self.board.snapshot(),
        }

    def opening(self) -> list[dict[str, Any]]:
        """What a browser that connects now gets first: the live state, or the replay so far."""
        if self.player.active:
            return [
                *self.player.played,
                {"type": "mode", "mode": "replay"},
                self.player.status(),
            ]
        return [self.bus.redactor.scrub(self.hello())]


async def _refresh_forever(state: State) -> None:
    while True:
        await asyncio.sleep(STACK_REFRESH_S)
        with contextlib.suppress(Exception):
            await state.columns.refresh()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    doors.install()  # what each backend sends to its resource is seen where it leaves
    state = State()
    app.state.demo = state
    await state.columns.start()
    state.tasks = [
        asyncio.create_task(state.board.poll_forever()),
        asyncio.create_task(_refresh_forever(state)),
    ]
    try:
        yield
    finally:
        for task in state.tasks:
            task.cancel()
        await state.columns.close()


app = FastAPI(title="interCEde presentation", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _state() -> State:
    state: State = app.state.demo
    return state


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/events")
async def events() -> StreamingResponse:
    state = _state()

    async def stream() -> AsyncIterator[str]:
        queue = state.bus.listen()
        try:
            for event in state.opening():
                yield _sse(event)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield _sse(event)
        finally:
            state.bus.unlisten(queue)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    """What the views draw from the code itself: payloads, states, the consumer's source."""
    return {
        "payloads": [
            {"name": p.name, "title": p.title, "blurb": p.blurb, "script": p.script}
            for p in PAYLOADS.values()
        ],
        "statuses": [status.value for status in JobStatus],
        "consumer": {"path": "demo/consumer.py", "source": inspect.getsource(consumer)},
        "scenarios": [
            {"name": name, "title": title, "needs": needs}
            for name, (title, needs) in SCENARIOS.items()
        ],
    }


@app.get("/api/architecture")
async def architecture() -> dict[str, Any]:
    return views.architecture()


@app.get("/api/composition")
async def composition() -> dict[str, Any]:
    return views.composition()


@app.get("/api/ci")
async def ci() -> Any:
    """The CI snapshot: as fetch_ci.py or the refresh button last wrote it."""
    path = fetch_ci.OUT
    return json.loads(path.read_text()) if path.is_file() else {"missing": True}


_ci_refreshing = asyncio.Lock()


@app.post("/api/ci/refresh")
async def ci_refresh() -> Any:
    """Read the latest integration runs from GitHub now, and keep them as the snapshot.

    The token stays on this host: it is registered with the redactor first, so
    no event, view or recording can ever hold it.
    """
    state = _state()
    if secret := fetch_ci.token():
        state.bus.redactor.add(secret)
    if _ci_refreshing.locked():
        raise HTTPException(409, "already checking")
    async with _ci_refreshing:
        state.bus.publish({"type": "ci.status", "state": "checking"})
        try:
            snapshot = await asyncio.to_thread(fetch_ci.fetch, CI_RUNS)
        except fetch_ci.RateLimitError as exc:
            when = time.strftime("%H:%M", time.localtime(exc.reset))
            message = f"GitHub's rate limit is used up until {when}"
        except OSError as exc:  # no network, or GitHub refused
            message = f"GitHub could not be reached: {exc}"
        else:
            fetch_ci.save(snapshot)
            state.bus.publish(
                {"type": "ci.status", "state": "done", "snapshot": snapshot}
            )
            return snapshot
    state.bus.publish({"type": "ci.status", "state": "failed", "message": message})
    raise HTTPException(502, message)


class TranslateRequest(BaseModel):
    form: dict[str, Any]
    launcher: bool = False


@app.post("/api/translate")
async def translate(request: TranslateRequest) -> dict[str, Any]:
    return await dryrun.translate(request.form, request.launcher)


class SubmitRequest(BaseModel):
    columns: list[str]
    payload: str
    copies: int = 3


@app.post("/api/submit")
async def submit(request: SubmitRequest) -> dict[str, str]:
    _live_only(_state())
    state = _state()
    if request.payload not in PAYLOADS or not 1 <= request.copies <= 50:
        raise HTTPException(400, "unknown payload, or copies not between 1 and 50")
    asyncio.create_task(
        state.board.submit(request.columns, request.payload, request.copies)
    )
    return {"status": "submitting"}


class ActionRequest(BaseModel):
    operation: str
    keys: list[str]


@app.post("/api/action")
async def action(request: ActionRequest) -> dict[str, Any]:
    _live_only(_state())
    if request.operation not in ("kill", "purge", "get_output", "get_diagnostics"):
        raise HTTPException(400, "unknown operation")
    return await _state().board.act(request.operation, request.keys)


@app.post("/api/reset")
async def reset() -> dict[str, str]:
    _live_only(_state())
    state = _state()
    state.scenarios.last.clear()
    await state.board.reset()
    return {"status": "reset"}


class ColumnRequest(BaseModel):
    column: str


@app.post("/api/restart")
async def restart(request: ColumnRequest) -> dict[str, Any]:
    _live_only(_state())
    state = _state()
    if request.column not in state.columns.columns:
        raise HTTPException(400, "unknown column")
    return await state.scenarios.restart(request.column)


class KeyRequest(BaseModel):
    key: str


@app.post("/api/forge")
async def forge(request: KeyRequest) -> dict[str, Any]:
    _live_only(_state())
    state = _state()
    job = state.board.jobs.get(request.key)
    if job is None or job.handle is None:
        raise HTTPException(400, "choose a submitted job first")
    return await state.scenarios.forge(request.key)


class ScenarioRequest(BaseModel):
    name: str
    column: str


@app.post("/api/scenario")
async def scenario(request: ScenarioRequest) -> dict[str, str]:
    _live_only(_state())
    state = _state()
    if request.name not in SCENARIOS or request.column not in state.columns.columns:
        raise HTTPException(400, "unknown scenario or column")
    if state.scenarios.running:
        raise HTTPException(409, "a scenario is already running")
    asyncio.create_task(state.scenarios.run(request.name, request.column))
    return {"status": "started"}


class StackRequest(BaseModel):
    stack: str
    action: str = "up"


@app.post("/api/stack")
async def stack(request: StackRequest) -> dict[str, str]:
    """Start or stop a stack, as docs/dev/how-to/run-integration-tests-locally.md does."""
    _live_only(_state())
    state = _state()
    if request.stack not in stacks() or request.action not in ("up", "down"):
        raise HTTPException(400, "unknown stack or action")
    asyncio.create_task(_compose(state, request.stack, request.action))
    return {"status": f"{request.action} started"}


async def _compose(state: State, stack_id: str, action: str) -> None:
    argv = ["docker", "compose", "-f", str(compose_file(stack_id)), action]
    if action == "up":
        argv.append("--wait")
    bus = state.bus
    bus.publish(
        {
            "type": "alert",
            "column": stack_id,
            "level": "info",
            "message": f"$ {' '.join(argv[3:])}: running",
        }
    )
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await process.communicate()
    level = "info" if process.returncode == 0 else "error"
    tail = out.decode(errors="replace").strip().splitlines()[-1:] or [""]
    bus.publish(
        {
            "type": "alert",
            "column": stack_id,
            "level": level,
            "message": f"{action}: exit {process.returncode} {tail[0]}",
        }
    )
    await state.columns.refresh()


class ModeRequest(BaseModel):
    mode: str


@app.post("/api/mode")
async def mode(request: ModeRequest) -> dict[str, str]:
    if request.mode not in ("live", "fake", "replay"):
        raise HTTPException(400, "unknown mode")
    state = _state()
    state.mode = request.mode
    if request.mode != "replay" and (state.player.active or not state.bus.live):
        # Back to the live state: every browser is sent all of it again.
        state.player.stop()
        state.bus.live = True
        state.bus.publish(state.hello())
    state.bus.publish(
        {"type": "mode", "mode": state.mode}, replayed=state.player.active
    )
    return {"mode": state.mode}


class RecordRequest(BaseModel):
    on: bool


@app.post("/api/record")
async def record(request: RecordRequest) -> dict[str, Any]:
    state = _state()
    _live_only(state)
    saved = None
    if request.on and not state.recorder.on:
        state.recorder.start(state.hello())
    elif not request.on and state.recorder.on:
        saved = state.recorder.stop().name
    state.bus.publish({"type": "recording", "on": state.recorder.on, "saved": saved})
    return {"on": state.recorder.on, "saved": saved}


@app.get("/api/recordings")
async def list_recordings() -> list[dict[str, Any]]:
    return recordings()


class ReplayRequest(BaseModel):
    action: str
    name: str | None = None
    speed: float | None = None


@app.post("/api/replay")
async def replay(request: ReplayRequest) -> dict[str, Any]:
    state = _state()
    player = state.player
    if request.action == "play" and request.name:
        if state.recorder.on:
            raise HTTPException(409, "stop recording first")
        state.mode = "replay"
        player.play(request.name)
    elif request.action == "pause":
        player.paused = True
    elif request.action == "resume":
        player.paused = False
    elif request.action == "speed" and request.speed:
        player.speed = max(0.25, min(request.speed, 16.0))
    else:
        raise HTTPException(400, "unknown replay action")
    status = player.status()
    state.bus.publish(status, replayed=True)
    return status


def _live_only(state: State) -> None:
    """Refuse what would act on a live backend while a recording plays."""
    if state.player.active:
        raise HTTPException(409, "a recording is playing: switch to Live or Fake first")


@app.get("/api/file")
async def file(path: str) -> FileResponse:
    """Serve a file a get_output wrote; nothing outside the collected output."""
    output = (RUNTIME / "output").resolve()
    target = (RUNTIME / path).resolve()
    if not target.is_relative_to(output) or not target.is_file():
        raise HTTPException(404, "no such collected file")
    return FileResponse(target, media_type="text/plain")


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
