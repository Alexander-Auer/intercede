"""What a backend sends to its resource, seen where it leaves the process.

A backend reaches its resource through one of a few doors: it runs a program
(a subprocess), or it makes an HTTP request (httpx). A composed backend goes
through its transport, which the demo already wraps at the ``Transport``
protocol. ``install()`` puts a watcher on each of the other doors, so a backend
is traced without the demo knowing anything about it: whatever crosses a door
during a contract call becomes a step of that call. Nothing is changed on its
way: the backend runs as it would without the demo.

In a dry run (``standing_in()``) the doors stand in for the resource: nothing
is run and nothing is sent, every call is recorded, and every answer is empty,
so ``submit()`` goes as far as it can without one. Any other connection is
refused (an audit hook), so a backend that reaches its resource some other way
fails its dry run instead of reaching a real resource.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from xml.dom import minidom
from xml.parsers.expat import ExpatError

from demo import tracing

_standing_in: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "intercede_demo_standing_in", default=False
)
_installed = False

STAND_IN_ANSWER = ""


@contextlib.contextmanager
def standing_in() -> Iterator[None]:
    """Within this block, the doors record every call and let none out."""
    token = _standing_in.set(True)
    try:
        yield
    finally:
        _standing_in.reset(token)


def install() -> None:
    """Watch every door; idempotent. The app and the dry run call it at start."""
    global _installed
    if _installed:
        return
    _installed = True
    _watch_subprocesses()
    _watch_http()
    sys.addaudithook(_refuse_other_connections)


def _watched() -> bool:
    """Tell whether a call at a door now belongs to a traced operation."""
    if tracing.inside_a_traced_part():
        return False
    return _standing_in.get() or tracing.current_operation() is not None


def _refuse_other_connections(event: str, args: tuple[Any, ...]) -> None:
    if event == "socket.connect" and _standing_in.get():
        raise ConnectionRefusedError("a dry run reaches no resource")


# -- a program -----------------------------------------------------------------------


def _watch_subprocesses() -> None:
    original = asyncio.create_subprocess_exec

    async def create_subprocess_exec(program: Any, *args: Any, **kwargs: Any) -> Any:
        if not _watched():
            return await original(program, *args, **kwargs)
        argv = [str(program), *map(str, args)]
        started = time.time()
        detail: dict[str, Any] = {
            "argv": argv,
            "env": _set_by_the_backend(kwargs.get("env")),
        }
        # Files named on the command line, read now: the backend may remove them.
        carried = _files_named(argv[1:])
        sent = "$ " + " ".join(argv)
        if _standing_in.get():
            return _Process(_StandIn(), argv[0], sent, carried, detail, started)
        try:
            process = await original(program, *args, **kwargs)
        except Exception as exc:
            tracing.record(
                "process",
                Path(argv[0]).name,
                started,
                {
                    **detail,
                    **tracing.exchange(sent, "failed", carried=carried),
                    **tracing._raised(exc),
                },
            )
            raise
        return _Process(process, argv[0], sent, carried, detail, started)

    asyncio.create_subprocess_exec = create_subprocess_exec  # type: ignore[assignment]
    asyncio.subprocess.create_subprocess_exec = create_subprocess_exec  # type: ignore[assignment]


def _set_by_the_backend(env: Mapping[str, str] | None) -> dict[str, str]:
    """What of a program's environment the backend chose: never the rest of os.environ."""
    if env is None:
        return {}
    return {k: v for k, v in env.items() if os.environ.get(k) != v}


def _files_named(args: list[str]) -> list[dict[str, Any]]:
    files = []
    for arg in args:
        path = Path(arg)
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
            entry: dict[str, Any] = {"name": path.name, "bytes": size}
            if size <= tracing.MAX_FILE:
                entry["text"] = path.read_text()
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        files.append(entry)
    return files


class _StandIn:
    """A program that was never started: it printed nothing and succeeded."""

    returncode: int | None = None
    pid = 0
    stdin = stdout = stderr = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.returncode = 0
        return STAND_IN_ANSWER.encode(), b""

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15

    def send_signal(self, signal: int) -> None:
        self.returncode = -signal


class _Process:
    """A running program, watched: its answer becomes a step when it has given it."""

    def __init__(
        self,
        inner: Any,
        program: str,
        sent: str,
        carried: list[dict[str, Any]],
        detail: dict[str, Any],
        started: float,
    ) -> None:
        self._inner = inner
        self._call = Path(program).name
        self._sent = sent
        self._carried = carried
        self._detail = detail
        self._started = started
        self._recorded = False
        # The step belongs to the operation that started the program.
        self._context = contextvars.copy_context()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        try:
            out, err = await self._inner.communicate(input)
        except BaseException as exc:
            self._record(None, "", "", exc)
            raise
        self._record(
            self._inner.returncode,
            out.decode(errors="replace") if out else "",
            err.decode(errors="replace") if err else "",
        )
        return out, err

    async def wait(self) -> int:
        code = int(await self._inner.wait())
        self._record(code, None, None)
        return code

    def _record(
        self,
        code: int | None,
        out: str | None,
        err: str | None,
        exc: BaseException | None = None,
    ) -> None:
        if self._recorded:
            return
        self._recorded = True
        answer = f"exit {code}" if code is not None else "no answer"
        detail = {
            **self._detail,
            **tracing.exchange(
                self._sent, answer, said=out, said_err=err, carried=self._carried
            ),
        }
        if code is not None:
            detail.update(
                {
                    "returncode": code,
                    "stdout": tracing._cut(out or "", tracing.MAX_TEXT),
                    "stderr": tracing._cut(err or "", tracing.MAX_TEXT),
                }
            )
        if exc is not None:
            detail.update({"raised": type(exc).__name__, "message": str(exc)})
        self._context.run(tracing.record, "process", self._call, self._started, detail)


# -- an HTTP request ------------------------------------------------------------------


def _watch_http() -> None:
    try:
        import httpx
    except ImportError:  # no backend of this version speaks HTTP
        return
    original = httpx.AsyncHTTPTransport.handle_async_request

    async def handle_async_request(self: Any, request: Any) -> Any:
        if not _watched():
            return await original(self, request)
        started = time.time()
        body = await request.aread()
        target = request.url.raw_path.decode(errors="replace")
        sent = f"{request.method} {target}"
        carried = _body("request body", body, request.headers.get("content-type"))
        detail: dict[str, Any] = {"method": request.method, "url": str(request.url)}
        if _standing_in.get():
            response = httpx.Response(
                200, content=STAND_IN_ANSWER.encode(), request=request
            )
        else:
            try:
                response = await original(self, request)
            except Exception as exc:
                tracing.record(
                    "http",
                    request.method,
                    started,
                    {
                        **detail,
                        **tracing.exchange(sent, "failed", carried=carried),
                        **tracing._raised(exc),
                    },
                )
                raise
        answer = await response.aread()
        text = _readable(
            answer.decode(errors="replace"), response.headers.get("content-type")
        )
        tracing.record(
            "http",
            request.method,
            started,
            {
                **detail,
                "status": response.status_code,
                **tracing.exchange(
                    sent,
                    f"{response.status_code} {response.reason_phrase}".strip(),
                    said=text,
                    carried=carried,
                ),
            },
        )
        return response

    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request  # type: ignore[method-assign]


def _body(name: str, content: bytes, content_type: str | None) -> list[dict[str, Any]]:
    if not content:
        return []
    entry: dict[str, Any] = {
        "name": f"{name} ({content_type or 'no type'})",
        "bytes": len(content),
    }
    if len(content) <= tracing.MAX_FILE:
        try:
            entry["text"] = _readable(content.decode(), content_type)
        except UnicodeDecodeError:
            pass
    return [entry]


def _readable(text: str, content_type: str | None) -> str:
    """Indent a JSON or XML body, as its content type says it is, for reading on a slide."""
    kind = (content_type or "").lower()
    try:
        if "json" in kind:
            return json.dumps(json.loads(text), indent=2)
        if "xml" in kind:
            return minidom.parseString(text).toprettyxml(indent="  ").split("\n", 1)[1]  # noqa: S318  (what the backend itself sent or got)
    except (ValueError, ExpatError):
        pass
    return text
