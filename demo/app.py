"""The presentation server: one event stream to the browser, and the actions a presenter takes."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from demo import consumer, dryrun, views
from demo.board import Board
from demo.columns import Columns, compose_file, stacks
from demo.events import Bus
from demo.payloads import PAYLOADS
from demo.scenarios import SCENARIOS, Scenarios
from demo.tracing import Tracer
from intercede import JobStatus

HERE = Path(__file__).parent
STATIC = HERE / "static"
RUNTIME = HERE / ".runtime"  # payload scripts and collected output; git-ignored
STACK_REFRESH_S = 5.0
HEARTBEAT_S = 15.0


class State:
    """Everything the server holds; built at start-up, closed at shutdown."""

    def __init__(self) -> None:
        self.bus = Bus()
        self.tracer = Tracer(self.bus)
        self.columns = Columns(self.tracer)
        self.board = Board(self.columns, RUNTIME)
        self.scenarios = Scenarios(self.board)
        self.mode = "live"
        self.tasks: list[asyncio.Task[None]] = []

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "mode": self.mode,
            "columns": self.columns.snapshot(),
            "operations": [op.as_event() for op in self.tracer.operations],
            "last": dict(self.scenarios.last),
            **self.board.snapshot(),
        }


async def _refresh_forever(state: State) -> None:
    while True:
        await asyncio.sleep(STACK_REFRESH_S)
        with contextlib.suppress(Exception):
            await state.columns.refresh()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
            yield _sse(state.bus.redactor.scrub(state.hello()))
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
    """The CI snapshot demo/scripts/fetch_ci.py wrote; the demo itself fetches nothing."""
    path = HERE / "data" / "ci.json"
    return json.loads(path.read_text()) if path.is_file() else {"missing": True}


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
    if request.operation not in ("kill", "purge", "get_output", "get_diagnostics"):
        raise HTTPException(400, "unknown operation")
    return await _state().board.act(request.operation, request.keys)


@app.post("/api/reset")
async def reset() -> dict[str, str]:
    state = _state()
    state.scenarios.last.clear()
    await state.board.reset()
    return {"status": "reset"}


class ColumnRequest(BaseModel):
    column: str


@app.post("/api/restart")
async def restart(request: ColumnRequest) -> dict[str, Any]:
    state = _state()
    if request.column not in state.columns.columns:
        raise HTTPException(400, "unknown column")
    return await state.scenarios.restart(request.column)


class KeyRequest(BaseModel):
    key: str


@app.post("/api/forge")
async def forge(request: KeyRequest) -> dict[str, Any]:
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
    state.bus.publish({"type": "mode", "mode": state.mode})
    return {"mode": state.mode}


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
