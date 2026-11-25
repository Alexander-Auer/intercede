"""Record how one call travels through interCEde, and print it as JSON.

Run by the checkout's own Python, as ``extract.py`` is, so it is that branch's code:

    python trace.py --list                  where a call can be traced, and which
    python trace.py <target> <operation>    one call, recorded

The targets name no backend: they are the fake the registry provides, which needs
no resource, and each stack of ``tests/integration/stacks.yml`` whose backend this
version provides, built as the integration suite builds it (its client
configuration, the credential it minted at start, the registry). So a call goes
all the way, to the real resource of a running stack and back with its answer.

Every function of ``intercede`` that runs is recorded with ``sys.monitoring``
(Python 3.12): its arguments, the line it was called from, what it returned or
raised, and the line it left from. What leaves the process is recorded where it
leaves, whichever backend sends it: a program started, an HTTP request, a command
or a file copy over SSH, each with what it answered. Nothing else is: the standard
library and the libraries are where the call goes, not part of interCEde.

A call made from a task (``asyncio.gather``) or a thread (``asyncio.to_thread``)
belongs to the call that started it: the current call is a context variable,
which a task and a thread inherit.

An operation that needs a job first gets one the way the integration suite gets
it: a job is submitted, unrecorded, and waited for until it runs or has ended.
Afterwards it is purged, so the stack is left as it was.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import os
import reprlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
from collections.abc import Callable
from pathlib import Path
from types import CodeType, FrameType
from typing import Any

REPO = Path.cwd()
sys.path.insert(
    0, str(REPO)
)  # tests.integration: the stacks, read as the suite reads them

OPERATIONS = ("submit", "get_status", "get_output", "kill", "purge", "get_diagnostics")
_GENERATORS = inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR
FAKE = "fake"  # the registry's own backend, with no resource behind it


def short(value: Any) -> str:
    """A value as it is shown: its repr, cut short; secrets stay hidden, as their repr does."""
    shown = reprlib.Repr(maxstring=160, maxother=200, maxlist=8, maxdict=8, maxlevel=3)
    try:
        return shown.repr(value)
    except Exception as exc:  # noqa: BLE001  (a repr that fails is shown as such)
        return f"<{type(value).__name__}: repr failed: {exc}>"


def say(text: str) -> None:
    """Tell the app what is happening, while it happens."""
    print(text, file=sys.stderr, flush=True)


# -- the recorder --------------------------------------------------------------------------


class Recorder:
    """Records the calls of one package's functions, and what leaves the process."""

    def __init__(self, package: Path) -> None:
        self.package = str(package) + os.sep
        self.nodes: list[dict[str, Any]] = []
        self.frames: dict[int, FrameType] = {}  # node -> its frame, while it runs
        self.by_frame: dict[int, int] = {}  # id(frame) -> node, while it runs
        self.current: contextvars.ContextVar[int | None] = contextvars.ContextVar(
            "current", default=None
        )
        self.on = False  # what leaves the process is recorded only while recording
        self.step = 0
        self.started = 0.0
        self.tool = sys.monitoring.PROFILER_ID

    def _now(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 3)

    def _next(self) -> int:
        self.step += 1
        return self.step

    def _ours(self, code: CodeType) -> bool:
        return (
            code.co_filename.startswith(self.package)
            and not code.co_flags & _GENERATORS
            and code.co_name != "<genexpr>"
        )

    def __enter__(self) -> Recorder:
        events = sys.monitoring.events
        sys.monitoring.use_tool_id(self.tool, "intercede-trace")
        sys.monitoring.register_callback(self.tool, events.PY_START, self._start)
        sys.monitoring.register_callback(self.tool, events.PY_RETURN, self._return)
        sys.monitoring.register_callback(self.tool, events.PY_UNWIND, self._unwind)
        self.started = time.perf_counter()
        self.on = True
        sys.monitoring.set_events(
            self.tool, events.PY_START | events.PY_RETURN | events.PY_UNWIND
        )
        return self

    def __exit__(self, *exc: object) -> None:
        sys.monitoring.set_events(self.tool, 0)
        sys.monitoring.free_tool_id(self.tool)
        self.on = False

    def _start(self, code: CodeType, offset: int) -> Any:
        if not self._ours(code):
            return sys.monitoring.DISABLE
        frame = sys._getframe(1)  # noqa: SLF001  (the frame that starts)
        parent = self.current.get()
        caller = self.frames.get(parent) if parent is not None else None
        count = code.co_argcount + code.co_kwonlyargcount
        count += bool(code.co_flags & inspect.CO_VARARGS)
        count += bool(code.co_flags & inspect.CO_VARKEYWORDS)
        args = {}
        for name in code.co_varnames[:count]:
            value = frame.f_locals.get(name)
            args[name] = (
                f"<{type(value).__name__}>" if name in ("self", "cls") else short(value)
            )
        node = {
            "id": len(self.nodes),
            "parent": parent,
            "kind": "call",
            "name": code.co_qualname.replace(".<locals>.", " › "),
            "qualname": code.co_qualname,
            "module": frame.f_globals.get("__name__", ""),
            "file": os.path.relpath(code.co_filename, REPO),
            "first_line": code.co_firstlineno,
            "call_line": caller.f_lineno if caller is not None else None,
            "args": args,
            "async": bool(code.co_flags & inspect.CO_COROUTINE),
            "start": self._now(),
            "step_in": self._next(),
        }
        self.nodes.append(node)
        self.frames[node["id"]] = frame
        self.by_frame[id(frame)] = node["id"]
        self.current.set(node["id"])
        return None

    def _end(self, code: CodeType, *, unwinding: bool = False, **outcome: Any) -> Any:
        if not self._ours(code):
            # PY_UNWIND cannot be switched off for one function, as the others can.
            return None if unwinding else sys.monitoring.DISABLE
        frame = sys._getframe(2)  # noqa: SLF001  (the frame that ends)
        number = self.by_frame.pop(id(frame), None)
        if number is None:
            return None
        node = self.nodes[number]
        node.update(
            outcome, end=self._now(), step_out=self._next(), return_line=frame.f_lineno
        )
        self.frames.pop(number, None)
        self.current.set(node["parent"])
        return None

    def _return(self, code: CodeType, offset: int, value: Any) -> Any:
        return self._end(code, returned=short(value))

    def _unwind(self, code: CodeType, offset: int, exc: BaseException) -> Any:
        return self._end(code, unwinding=True, raised=f"{type(exc).__name__}: {exc}")

    def external(self, kind: str, title: str, sent: str = "") -> dict[str, Any] | None:
        """What leaves the process: a command, a request, a copy; ``answered`` completes it."""
        if not self.on:
            return None
        parent = self.current.get()
        caller = self.frames.get(parent) if parent is not None else None
        node = {
            "id": len(self.nodes),
            "parent": parent,
            "kind": kind,
            "name": title,
            "sent": sent,
            "call_line": caller.f_lineno if caller is not None else None,
            "start": self._now(),
            "step_in": self._next(),
        }
        self.nodes.append(node)
        return node

    def answered(
        self,
        node: dict[str, Any] | None,
        answer: str,
        detail: str = "",
        *,
        failed: bool = False,
    ) -> None:
        if node is not None:
            node.update(
                returned=answer,
                detail=detail,
                failed=failed,
                end=self._now(),
                step_out=self._next(),
            )


# -- the doors: where a call leaves the process, whichever backend makes it ------------------------


def _text(data: Any) -> str:
    if isinstance(data, bytes):
        return data.decode(errors="replace").strip()
    return str(data or "").strip()


def _answer(code: int | None, out: str, err: str) -> tuple[str, str]:
    first = out.splitlines()[0] if out else ""
    return f"exit {code}" + (f": {first}" if first else ""), "\n".join(
        part for part in (out, err) if part
    )


def watch_processes(recorder: Recorder) -> None:
    """A program started on this host."""
    start = asyncio.create_subprocess_exec

    async def recorded(program: str, *args: str, **kwargs: Any) -> Any:
        node = recorder.external("command", shlex.join([program, *args]))
        try:
            process = await start(program, *args, **kwargs)
        except OSError as exc:  # not found, not allowed: it never ran
            recorder.answered(node, f"not started: {exc.strerror or exc}", failed=True)
            raise
        communicate = process.communicate

        async def answered(input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
            out, err = await communicate(input)
            answer, detail = _answer(process.returncode, _text(out), _text(err))
            recorder.answered(node, answer, detail, failed=process.returncode != 0)
            return out, err

        process.communicate = answered  # type: ignore[method-assign]
        return process

    asyncio.create_subprocess_exec = recorded  # type: ignore[assignment]


def watch_http(recorder: Recorder) -> None:
    """An HTTP request, where httpx sends it."""
    try:
        import httpx
    except ImportError:
        return
    handle = httpx.AsyncHTTPTransport.handle_async_request

    async def recorded(self: Any, request: Any) -> Any:
        body = (await request.aread()).decode(errors="replace")
        node = recorder.external(
            "request", f"{request.method} {request.url}", body[:2000]
        )
        response = await handle(self, request)
        if node is not None:
            await response.aread()
            recorder.answered(
                node,
                f"{response.status_code} {response.reason_phrase}".strip(),
                response.content.decode(errors="replace")[:2000],
                failed=response.status_code >= 400,
            )
        return response

    httpx.AsyncHTTPTransport.handle_async_request = recorded  # type: ignore[method-assign]


def watch_ssh(recorder: Recorder) -> None:
    """A command run, or a file copied, over SSH, where asyncssh does it."""
    try:
        import asyncssh
    except ImportError:
        return
    create = asyncssh.SSHClientConnection.create_process

    async def create_process(
        self: Any, command: str = "", *args: Any, **kwargs: Any
    ) -> Any:
        node = recorder.external("command", f"ssh: {command}")
        process = await create(self, command, *args, **kwargs)
        wait = process.wait

        async def waited(*a: Any, **k: Any) -> Any:
            done = await wait(*a, **k)
            answer, detail = _answer(
                done.returncode, _text(done.stdout), _text(done.stderr)
            )
            recorder.answered(node, answer, detail, failed=done.returncode != 0)
            return done

        process.wait = waited
        return process

    asyncssh.SSHClientConnection.create_process = create_process  # type: ignore[method-assign]

    for name in ("put", "get"):
        original = getattr(asyncssh.SFTPClient, name)

        def copy(
            original: Callable[..., Any] = original, name: str = name
        ) -> Callable[..., Any]:
            async def copied(
                self: Any, sources: Any, target: Any = None, **kwargs: Any
            ) -> Any:
                listed = sources if isinstance(sources, list | tuple) else [sources]
                files = " ".join(str(s) for s in listed)
                node = recorder.external("copy", f"sftp {name} {files} → {target}")
                try:
                    result = await original(self, sources, target, **kwargs)
                except Exception as exc:
                    recorder.answered(node, f"failed: {exc}", failed=True)
                    raise
                recorder.answered(node, "copied")
                return result

            return copied

        setattr(asyncssh.SFTPClient, name, copy())


# -- where a call can be traced ------------------------------------------------------------


def _stacks() -> dict[str, dict[str, Any]]:
    """The stacks of the integration suite, by id; none on a branch without it."""
    try:
        from tests.integration.generate_matrix import load_manifest
    except ImportError:
        return {}
    return {entry["id"]: entry for entry in load_manifest()}


def _stack_folder(entry: dict[str, Any]) -> Path:
    from tests.integration.generate_matrix import STACKS_DIR, compose_env

    return STACKS_DIR / compose_env(entry)


def _client_config(stack: str) -> dict[str, Any] | None:
    from tests.integration.generate_matrix import STACKS_DIR

    path = STACKS_DIR / stack / "config" / "basic" / "intercede-client.toml"
    if not path.is_file():
        return None
    with path.open("rb") as f:
        return tomllib.load(f)


def _running(entry: dict[str, Any]) -> bool | None:
    """Whether a stack's containers are up; None when Docker cannot say."""
    docker = shutil.which("docker")
    if docker is None:
        return None
    compose = _stack_folder(entry) / "compose.yml"
    done = subprocess.run(  # noqa: S603  (our own compose file)
        [docker, "compose", "-f", str(compose), "ps", "--status", "running", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return bool(done.stdout.strip()) if done.returncode == 0 else None


def _operations(cls: type) -> list[str]:
    from intercede import protocols

    offered = {
        "get_output": protocols.OutputRetriever,
        "kill": protocols.Killable,
        "purge": protocols.Purgeable,
        "get_diagnostics": protocols.Diagnosable,
    }
    found = ["submit", "get_status"]
    for operation, protocol in offered.items():
        members = {name for name in dir(protocol) if not name.startswith("_")}
        if all(callable(getattr(cls, name, None)) for name in members):
            found.append(operation)
    return found


def _class_of(kind: str) -> type:
    """The class the registry builds for ``kind``; only its class: nothing is validated."""
    from intercede import registry

    return registry._load("backend", kind)  # noqa: SLF001  (the class, not built)


def available() -> list[dict[str, Any]]:
    """The fake, and each stack this version provides and this host reaches."""
    from intercede import registry

    provided = registry.available()["backend"]
    found = []
    try:  # internal, for the test suites: not something a configuration asks for
        fake = _class_of(FAKE)
    except (KeyError, ImportError):
        fake = None
    if fake is not None:
        found.append(
            {
                "name": FAKE,
                "title": "The fake backend",
                "about": "The registry's own, in memory: nothing runs, nothing to start.",
                "operations": _operations(fake),
                "running": True,
            }
        )
    for stack, entry in _stacks().items():
        config = _client_config(stack)
        if config is None or config["backend"]["kind"] not in provided:
            continue  # this version does not provide its backend yet
        if entry.get("exec_in_container"):
            continue  # reached from inside its container only, not from this host
        backend = config["backend"]
        parts = [backend["kind"], backend.get("transport"), backend.get("scheduler")]
        compose = _stack_folder(entry) / "compose.yml"
        found.append(
            {
                "name": stack,
                "title": f"The {stack} stack",
                "about": f"{' + '.join(p for p in parts if p)}, on its integration stack: "
                "a real resource, in containers.",
                "operations": _operations(_class_of(backend["kind"])),
                "running": _running(entry),
                "start": f"docker compose -f {os.path.relpath(compose, REPO)} up --wait",
            }
        )
    return found


def _build(target: str) -> Any:
    """A backend for ``target``, built as the integration suite builds one; not yet open."""
    from intercede import registry
    from intercede.auth import StaticCredentials

    if (
        target == FAKE
    ):  # built as the unit tests build it: it has no configuration of its own
        from intercede._backends.config import FakeConfig

        return registry._build(FakeConfig(), StaticCredentials([]))  # noqa: SLF001
    from tests.integration.conftest import _READERS

    entry = _stacks()[target]
    config = _client_config(target)
    assert config is not None
    section = config.get("credentials")
    credentials = []
    if section is not None:
        path = _stack_folder(entry) / section["path"]
        if section["type"] == "ssh-key":  # the host key the stack made at its start
            config["backend"]["known_hosts"] = (path / "known_hosts").read_text()
        credentials.append(_READERS[section["type"]](path))
        certificates = (
            path / "certificates"
        )  # a TLS stack's CA, trusted as a grid host does
        if certificates.is_dir():
            present = [
                p for p in os.environ.get("X509_CERT_DIR", "").split(os.pathsep) if p
            ]
            os.environ["X509_CERT_DIR"] = os.pathsep.join([*present, str(certificates)])
    return registry.get_backend(config["backend"], StaticCredentials(credentials))


# -- running one call -------------------------------------------------------------------------


def _spec(folder: Path, seconds: int) -> Any:
    """A job like the integration suite's: a script of this host's, run on the resource."""
    from intercede.models import SubmissionSpec

    script = folder / ("long.sh" if seconds else "job.sh")
    script.write_text(
        "#!/bin/sh\necho 'hello from interCEde'\n"
        + (f"sleep {seconds}\n" if seconds else "")
    )
    script.chmod(0o755)
    return SubmissionSpec(executable=str(script))


async def _wait(backend: Any, handle: Any, wanted: set[str], limit: float) -> str:
    """Poll a job's status, unrecorded, until it is one of ``wanted``; give up after ``limit``."""
    started = time.monotonic()
    while True:
        status = (await backend.get_status([handle]))[handle]
        waited = time.monotonic() - started
        if status.name in wanted or waited > limit:
            return status.name
        say(f"The job is {status.name.lower()}; waiting for it ({waited:.0f} s)…")
        await asyncio.sleep(2)


async def trace(target: str, operation: str) -> dict[str, Any]:
    """Run ``operation`` on ``target``, recorded; first a job, if the operation needs one."""
    from intercede.protocols import Purgeable

    recorder = Recorder(REPO / "src" / "intercede")
    watch_processes(recorder)
    watch_http(recorder)
    watch_ssh(recorder)
    backend = _build(target)
    outcome: dict[str, Any] = {}
    leftover: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="intercede-trace-") as name:
        folder = Path(name)
        async with backend:
            handle = None
            if operation != "submit":
                active = operation in ("kill", "purge", "get_diagnostics")
                say("Submitting a job first, to have one…")
                handle = (
                    await backend.submit(_spec(folder, 300 if active else 0))
                ).handles[0]
                leftover.append(handle)
                if active:
                    outcome["job"] = await _wait(backend, handle, {"RUNNING"}, 90)
                elif operation == "get_output":
                    ended = {"DONE", "FAILED", "CANCELLED", "UNKNOWN"}
                    outcome["job"] = await _wait(backend, handle, ended, 180)
            spec = _spec(
                folder, 0
            )  # made before recording: the call starts at the backend
            say(f"Recording {operation}()…")
            with recorder:
                try:
                    if operation == "submit":
                        result = await backend.submit(spec)
                        leftover += list(result.handles.values())
                    elif operation == "get_output":
                        result = await backend.get_output([handle], folder / "out")
                    else:
                        result = await getattr(backend, operation)([handle])
                    outcome["result"] = short(result)
                except Exception as exc:  # noqa: BLE001  (shown in the trace)
                    outcome["error"] = f"{type(exc).__name__}: {exc}"
            if leftover and isinstance(backend, Purgeable):  # leave the stack as it was
                try:
                    await backend.purge(leftover)
                except Exception as exc:  # noqa: BLE001  (cleaning up only)
                    say(f"Purging the jobs afterwards failed: {exc}")
    return {
        "backend": target,
        "operation": operation,
        "class": type(backend).__name__,
        **outcome,
        "duration": max((n.get("end", 0) for n in recorder.nodes), default=0),
        "nodes": recorder.nodes,
    }


def main() -> int:
    if sys.version_info < (3, 12):
        json.dump(
            {"error": "tracing needs Python 3.12 or newer (sys.monitoring)"}, sys.stdout
        )
        return 1
    if sys.argv[1:] == ["--list"]:
        json.dump({"backends": available()}, sys.stdout)
        return 0
    target, operation = sys.argv[1:3]
    if operation not in OPERATIONS:
        json.dump({"error": f"unknown operation: {operation}"}, sys.stdout)
        return 1
    try:
        json.dump(asyncio.run(trace(target, operation)), sys.stdout)
    except Exception as exc:  # noqa: BLE001  (the app shows why)
        hint = "" if target == FAKE else f" Is the {target} stack up?"
        json.dump(
            {
                "error": f"{type(exc).__name__}: {exc}.{hint}",
                "trace": traceback.format_exc(limit=6),
            },
            sys.stdout,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
