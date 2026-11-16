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

from demo import consumer
from demo.board import Board
from demo.columns import Columns
from demo.events import Bus
from demo.payloads import PAYLOADS
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
        self.mode = "live"
        self.tasks: list[asyncio.Task[None]] = []

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "mode": self.mode,
            "columns": self.columns.snapshot(),
            "operations": [op.as_event() for op in self.tracer.operations],
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
        yield _sse(state.bus.redactor.scrub(state.hello()))
        source = state.bus.subscribe()
        pending = asyncio.ensure_future(anext(source))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_S)
                if not done:
                    yield ": heartbeat\n\n"
                    continue
                yield _sse(pending.result())
                pending = asyncio.ensure_future(anext(source))
        finally:
            pending.cancel()
            await source.aclose()

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
    }


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
    await _state().board.reset()
    return {"status": "reset"}


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
