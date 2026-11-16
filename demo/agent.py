"""Running a backend where it must run: the ``local-slurm`` column, inside the Slurm container.

A local transport runs commands on the host interCEde runs on, so local + Slurm
only works on a machine with Slurm: the stack's container. The integration suite
runs there for the same reason (``exec_in_container`` in stacks.yml).

``python -m demo.agent`` runs inside the container, started with ``docker compose
exec``, and serves a real ``BatchBackend`` built from the registry (local
transport, Slurm scheduler), traced with the same wrappers as everywhere else. It
reads one request per line on stdin and writes replies and trace steps on stdout.

``AgentBackend`` is its other end, on this host: a backend that forwards each
call over that pipe. It is what the board's consumer code calls, so the
consumer's side is the same as for every other column. Two things are carried
over the pipe that a backend in-process would not need: a copy's secrets (to the
container, never to the browser), and collected output files (back from it, since
the container writes them into its own file system).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sys
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from demo import tracing
from demo.events import Bus
from demo.tracing import Operation, Tracer
from intercede import exceptions
from intercede.models import (
    CopyVars,
    JobHandle,
    JobOutput,
    JobStatus,
    OpOutcome,
    StatusMap,
    Submission,
    SubmissionSpec,
)

# How the agent is started in the stack's container, as its unprivileged user.
CONTAINER_PYTHON = "/opt/suite/bin/python"
CONTAINER_PATH = "/intercede/src:/intercede"
CONFIG = {"kind": "batch", "transport": "local", "scheduler": "slurm"}


# -- in the container ------------------------------------------------------------


async def serve() -> None:
    """Serve one BatchBackend over stdin and stdout until stdin closes."""
    from intercede import registry
    from intercede._backends.batch.protocols import Scheduler, Transport
    from intercede.auth.providers import StaticCredentials
    from intercede.config import validate_config

    bus = Bus()
    tracer = Tracer(bus)
    asking: list[int] = [0]  # the request being served, for the steps it causes
    bus.tap(
        lambda e: _write({"id": asking[0], "step": e["step"]})
        if e["type"] == "op.step"
        else None
    )

    settings: Any = validate_config(CONFIG)
    provider = StaticCredentials()  # a local transport needs no credentials
    transport = tracing.TracingTransport(
        registry._load("transport", "local").from_settings(settings, provider), tracer
    )
    scheduler = tracing.TracingScheduler(
        registry._load("scheduler", "slurm").from_settings(settings), tracer
    )
    registry._checked("transport", "local", transport, Transport)
    registry._checked("scheduler", "slurm", scheduler, Scheduler)
    inner = registry._load("backend", "batch").from_settings(
        settings, transport=transport, scheduler=scheduler
    )
    backend = tracing.traced_backend(inner, "local-slurm", "LIVE", tracer)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    async with backend:
        _write(
            {
                "ready": True,
                "capabilities": tracing.capabilities(backend),
                "resource": backend.resource,
            }
        )
        while line := await reader.readline():
            request = json.loads(line)
            asking[0] = request["id"]
            try:
                result = await _handle(backend, request)
            except exceptions.InterCEdeError as exc:
                _write(
                    {
                        "id": request["id"],
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                )
            else:
                _write({"id": request["id"], "result": result})


async def _handle(backend: Any, request: dict[str, Any]) -> Any:
    op = request["op"]
    if op == "submit":
        spec = SubmissionSpec.model_validate(request["spec"])
        if (script := request.get("executable")) is not None:
            # The host's script, written here: the backend stages it during submit().
            with tempfile.TemporaryDirectory() as here:
                path = Path(here) / Path(spec.executable).name
                path.write_bytes(base64.b64decode(script))
                path.chmod(0o700)
                local = spec.model_copy(update={"executable": str(path)})
                return await _submit(backend, local, request["copies"])
        return await _submit(backend, spec, request["copies"])
    handles = [JobHandle.model_validate(h) for h in request["handles"]]
    if op == "get_status":
        statuses = await backend.get_status(handles)
        return [[h.model_dump(mode="json"), str(statuses[h])] for h in handles]
    if op == "get_output":
        with tempfile.TemporaryDirectory() as dest:
            outcomes = await backend.get_output(handles, Path(dest))
            return [[h.model_dump(mode="json"), _carried(outcomes[h])] for h in handles]
    outcomes = await getattr(backend, op)(handles)
    return [
        [
            h.model_dump(mode="json"),
            {"ok": outcomes[h].ok, "reason": outcomes[h].reason},
        ]
        for h in handles
    ]


async def _submit(backend: Any, spec: SubmissionSpec, raw: list[dict[str, Any]]) -> Any:
    copies = [
        CopyVars(
            environment=c["environment"],
            secrets={name: base64.b64decode(v) for name, v in c["secrets"].items()},
        )
        for c in raw
    ]
    return (await backend.submit(spec, copies)).model_dump(mode="json")


def _carried(outcome: OpOutcome[JobOutput]) -> dict[str, Any]:
    """An output outcome with its files' contents, since the host cannot read them here."""
    if not outcome.ok or outcome.value is None:
        return {"ok": outcome.ok, "reason": outcome.reason}
    read = lambda path: base64.b64encode(path.read_bytes()).decode() if path else None  # noqa: E731
    value = outcome.value
    return {
        "ok": True,
        "reason": outcome.reason,
        "streams": {
            "stdout": read(value.stdout),
            "stderr": read(value.stderr),
            "log": read(value.log),
        },
        "files": {name: read(path) for name, path in value.files.items()},
    }


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


# -- on this host --------------------------------------------------------------------


class AgentBackend:
    """A BatchBackend in the Slurm container, reached through the agent's pipe."""

    # What runs in the container, for the board: this class only carries the calls.
    shown_class = "BatchBackend (in the Slurm container)"

    def __init__(self, compose_file: Path, tracer: Tracer) -> None:
        self._compose = compose_file
        self._tracer = tracer
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, tuple[asyncio.Future[Any], Operation | None]] = {}
        self._next = 0
        self.resource = "localhost"
        self.capabilities: list[str] = []

    async def __aenter__(self) -> Self:
        self._process = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "-f",
            str(self._compose),
            "exec",
            "-T",
            "--user",
            "dirac",
            "-e",
            f"PYTHONPATH={CONTAINER_PATH}",
            "backend",
            CONTAINER_PYTHON,
            "-m",
            "demo.agent",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 2**20,  # collected output comes back as one line
        )
        assert self._process.stdout is not None
        first = await asyncio.wait_for(self._process.stdout.readline(), 30)
        if not first:
            assert self._process.stderr is not None
            error = (await self._process.stderr.read()).decode(errors="replace").strip()
            raise exceptions.ResourceUnavailableError(
                f"the agent did not start: {error[-300:]}"
            )
        hello = json.loads(first)
        self.capabilities, self.resource = hello["capabilities"], hello["resource"]
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._process is not None and self._process.returncode is None:
            assert self._process.stdin is not None
            self._process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._process.wait(), 5)
            if self._process.returncode is None:
                self._process.kill()
        if self._reader is not None:
            self._reader.cancel()
        self._process = None

    # -- the contract, forwarded ----------------------------------------------------

    async def submit(
        self, spec: SubmissionSpec, copies: int | list[CopyVars] = 1
    ) -> Submission:
        per_copy = [CopyVars()] * copies if isinstance(copies, int) else list(copies)
        # A script on this host is carried along: the container cannot read it here.
        script = Path(spec.executable)
        result = await self._ask(
            "submit",
            spec=spec.model_dump(mode="json"),
            executable=base64.b64encode(script.read_bytes()).decode()
            if script.is_file()
            else None,
            copies=[
                {
                    "environment": dict(c.environment),
                    "secrets": {
                        n: base64.b64encode(s.get_secret_value()).decode()
                        for n, s in c.secrets.items()
                    },
                }
                for c in per_copy
            ],
        )
        return Submission.model_validate(result)

    async def get_status(self, handles: list[JobHandle]) -> StatusMap:
        result = await self._ask(
            "get_status", handles=[h.model_dump(mode="json") for h in handles]
        )
        return {JobHandle.model_validate(h): JobStatus(s) for h, s in result}

    async def kill(self, handles: list[JobHandle]) -> dict[JobHandle, OpOutcome[None]]:
        return await self._outcomes("kill", handles)

    async def purge(self, handles: list[JobHandle]) -> dict[JobHandle, OpOutcome[None]]:
        return await self._outcomes("purge", handles)

    async def get_output(
        self, handles: list[JobHandle], dest: Path
    ) -> dict[JobHandle, OpOutcome[JobOutput]]:
        result = await self._ask(
            "get_output", handles=[h.model_dump(mode="json") for h in handles]
        )
        outcomes: dict[JobHandle, OpOutcome[JobOutput]] = {}
        for raw, carried in result:
            handle = JobHandle.model_validate(raw)
            if not carried["ok"]:
                outcomes[handle] = OpOutcome[JobOutput](
                    ok=False, reason=carried["reason"]
                )
                continue
            target = dest / handle.id
            target.mkdir(parents=True, exist_ok=True)

            def put(name: str, content: str | None, base: Path = target) -> Path | None:
                if content is None:
                    return None
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(content))
                return path

            streams = {k: put(k, v) for k, v in carried["streams"].items()}
            files = {
                name: put(f"files/{name}", content)
                for name, content in carried["files"].items()
            }
            outcomes[handle] = OpOutcome[JobOutput](
                ok=True,
                reason=carried["reason"],
                value=JobOutput(files=files, **streams),
            )
        return outcomes

    async def _outcomes(
        self, op: str, handles: list[JobHandle]
    ) -> dict[JobHandle, OpOutcome[None]]:
        result = await self._ask(
            op, handles=[h.model_dump(mode="json") for h in handles]
        )
        return {JobHandle.model_validate(h): OpOutcome[None](**o) for h, o in result}

    # -- the pipe ---------------------------------------------------------------

    async def _ask(self, op: str, **fields: Any) -> Any:
        if self._process is None or self._process.returncode is not None:
            raise exceptions.ResourceUnavailableError(
                "the agent in the Slurm container is not running"
            )
        self._next += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[self._next] = (future, tracing.current_operation())
        assert self._process.stdin is not None
        self._process.stdin.write(
            (json.dumps({"id": self._next, "op": op, **fields}) + "\n").encode()
        )
        await self._process.stdin.drain()
        return await future

    async def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        while line := await self._process.stdout.readline():
            message = json.loads(line)
            future, operation = self._pending.get(message.get("id"), (None, None))
            if "step" in message:
                self._tracer.attach(operation, message["step"])
            elif future is not None:
                del self._pending[message["id"]]
                if "error" in message:
                    error = getattr(
                        exceptions, message["error"]["type"], exceptions.InterCEdeError
                    )
                    future.set_exception(error(message["error"]["message"]))
                else:
                    future.set_result(message["result"])
        for future, _ in self._pending.values():  # the agent is gone
            if not future.done():
                future.set_exception(
                    exceptions.ResourceUnavailableError(
                        "the agent in the Slurm container stopped"
                    )
                )
        self._pending.clear()


if __name__ == "__main__":
    asyncio.run(serve())
