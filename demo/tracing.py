"""Tracing wrappers: real interCEde objects, wrapped so every call they make is seen.

Nothing here reimplements the library. Each wrapper delegates to the real object
and publishes what went in and what came out. Because interCEde's protocols are
structural, the wrappers satisfy the same ``isinstance`` checks the registry and
consumers use, and no more: a wrapped backend has exactly the capabilities of the
backend it wraps (see ``traced_backend``).

The redaction rule lives here too: whatever is secret (a credential handed out
by the provider, a copy's secrets) is registered with the bus before the call
that uses it, and files that hold secrets are listed by name only.
"""

from __future__ import annotations

import contextvars
import itertools
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from demo.events import Bus
from intercede.auth.models import (
    BearerToken,
    Credential,
    CredentialRequirements,
    SSHPassword,
    SSHPrivateKey,
)
from intercede.models import (
    CopyVars,
    JobHandle,
    OpOutcome,
    Submission,
    SubmissionSpec,
)
from intercede.protocols import (
    Diagnosable,
    Killable,
    OutputRetriever,
    Purgeable,
    StatusReporter,
    Submitter,
)

# Long tool output is cut to its end: the end is where a tool says what went wrong.
MAX_TEXT = 4000
MAX_FILE = 16_000
KEPT_OPERATIONS = 300

# The contract's operations, in the order the protocols define them.
CAPABILITIES: tuple[tuple[str, type], ...] = (
    ("submit", Submitter),
    ("get_status", StatusReporter),
    ("get_output", OutputRetriever),
    ("kill", Killable),
    ("purge", Purgeable),
    ("get_diagnostics", Diagnosable),
)

_ids = itertools.count(1)


@dataclass
class Operation:
    """One contract call made by the consumer, and everything it caused underneath."""

    id: int
    column: str
    source: str
    name: str
    started: float
    request: dict[str, Any]
    steps: list[dict[str, Any]] = field(default_factory=list)
    ended: float | None = None
    outcome: dict[str, Any] | None = None

    def as_event(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "column": self.column,
            "source": self.source,
            "name": self.name,
            "started": self.started,
            "ended": self.ended,
            "request": self.request,
            "steps": self.steps,
            "outcome": self.outcome,
        }


_current: contextvars.ContextVar[Operation | None] = contextvars.ContextVar(
    "intercede_demo_operation", default=None
)


class Tracer:
    """Publishes operations and their steps, and keeps the latest for new viewers."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.operations: deque[Operation] = deque(maxlen=KEPT_OPERATIONS)

    def clear(self) -> None:
        self.operations.clear()

    def start(
        self, column: str, source: str, name: str, request: dict[str, Any]
    ) -> Operation:
        operation = Operation(
            next(_ids),
            column,
            source,
            name,
            time.time(),
            self.bus.redactor.scrub(request),
        )
        self.operations.append(operation)
        self.bus.publish({"type": "op.start", "op": operation.as_event()})
        return operation

    def end(self, operation: Operation, outcome: dict[str, Any]) -> None:
        operation.ended = time.time()
        operation.outcome = self.bus.redactor.scrub(outcome)
        self.bus.publish(
            {
                "type": "op.end",
                "id": operation.id,
                "ended": operation.ended,
                "outcome": operation.outcome,
            }
        )

    def step(
        self, lane: str, call: str, started: float, detail: dict[str, Any]
    ) -> None:
        """Record one call below the contract, within the operation running now."""
        operation = _current.get()
        if operation is None:
            return  # opened or closed outside any operation: nothing to attach it to
        step = self.bus.redactor.scrub(
            {
                "lane": lane,
                "call": call,
                "started": started,
                "ms": round((time.time() - started) * 1000, 1),
                **detail,
            }
        )
        operation.steps.append(step)
        self.bus.publish({"type": "op.step", "id": operation.id, "step": step})


# -- the backend: what the consumer sees ------------------------------------


class _TracedBackend:
    """The part every traced backend shares; its operations are added per class."""

    def __init__(self, inner: Any, column: str, source: str, tracer: Tracer) -> None:
        self.inner = inner
        self.column = column
        self.source = source
        self._tracer = tracer

    @property
    def resource(self) -> str:
        return str(getattr(self.inner, "resource", ""))

    async def __aenter__(self) -> Self:
        await self.inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.inner.__aexit__(exc_type, exc, tb)


def _operation(name: str) -> Callable[..., Awaitable[Any]]:
    async def call(self: _TracedBackend, *args: Any, **kwargs: Any) -> Any:
        tracer = self._tracer
        if name == "submit":
            _register_secrets(
                tracer.bus, args[1] if len(args) > 1 else kwargs.get("copies", 1)
            )
        operation = tracer.start(
            self.column, self.source, name, _request(name, args, kwargs)
        )
        token = _current.set(operation)
        try:
            result = await getattr(self.inner, name)(*args, **kwargs)
        except Exception as exc:
            tracer.end(operation, {"raised": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            _current.reset(token)
        tracer.end(operation, _outcome(result))
        return result

    call.__name__ = name
    return call


def traced_backend(inner: Any, column: str, source: str, tracer: Tracer) -> Any:
    """Wrap a backend in a class that has exactly the capabilities it has.

    ``runtime_checkable`` protocols look attributes up statically since Python
    3.12, so a delegating ``__getattr__`` would pass no check at all. A class
    that defines every operation would pass every check, claiming capabilities
    the backend lacks. This builds the class per backend instead, from the same
    ``isinstance`` checks a consumer makes.
    """
    namespace = {
        name: _operation(name)
        for name, protocol in CAPABILITIES
        if isinstance(inner, protocol)
    }
    cls = type(f"Traced{type(inner).__name__}", (_TracedBackend,), namespace)
    return cls(inner, column, source, tracer)


def capabilities(backend: Any) -> list[str]:
    """Return the names of the protocols ``backend`` satisfies, in contract order."""
    return [
        protocol.__name__
        for _, protocol in CAPABILITIES
        if isinstance(backend, protocol)
    ]


# -- below the contract -----------------------------------------------------


class TracingTransport:
    """A composed backend's transport, with every command and copy seen."""

    def __init__(self, inner: Any, tracer: Tracer) -> None:
        self._inner = inner
        self._tracer = tracer

    @property
    def resource(self) -> str:
        return str(self._inner.resource)

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc, tb)

    async def run(self, argv: Sequence[str], *, timeout: float) -> Any:
        started = time.time()
        try:
            result = await self._inner.run(argv, timeout=timeout)
        except Exception as exc:
            self._tracer.step(
                "transport", "run", started, {"argv": list(argv), **_raised(exc)}
            )
            raise
        self._tracer.step(
            "transport", "run", started, {"argv": list(argv), **_result(result)}
        )
        return result

    async def put(self, local: Path, remote: str, *, timeout: float) -> None:
        started = time.time()
        files = _listing(local)
        try:
            await self._inner.put(local, remote, timeout=timeout)
        except Exception as exc:
            self._tracer.step(
                "transport",
                "put",
                started,
                {"remote": remote, "files": files, **_raised(exc)},
            )
            raise
        self._tracer.step(
            "transport", "put", started, {"remote": remote, "files": files}
        )

    async def get(self, remote: str, local: Path, *, timeout: float) -> None:
        started = time.time()
        try:
            await self._inner.get(remote, local, timeout=timeout)
        except Exception as exc:
            self._tracer.step(
                "transport", "get", started, {"remote": remote, **_raised(exc)}
            )
            raise
        size = local.stat().st_size if local.is_file() else None
        self._tracer.step(
            "transport", "get", started, {"remote": remote, "bytes": size}
        )


class TracingScheduler:
    """A composed backend's scheduler, with every command it builds and every parse seen.

    The scheduler is pure, so these steps take no time: what they show is the
    translation from the contract into one batch system's dialect.
    """

    def __init__(self, inner: Any, tracer: Tracer) -> None:
        self._inner = inner
        self._tracer = tracer

    @property
    def stages_own_files(self) -> bool:
        return bool(self._inner.stages_own_files)

    def is_job_id(self, value: str) -> bool:
        return bool(self._inner.is_job_id(value))  # asked per handle: too fine to show

    def submit_command(self, spec: SubmissionSpec, count: int, workdir: str) -> Any:
        started = time.time()
        command = self._inner.submit_command(spec, count, workdir)
        self._tracer.step(
            "scheduler",
            "submit_command",
            started,
            {
                "count": count,
                "argv": list(command.argv),
                "files": [
                    {"name": n, "text": _cut(t, MAX_FILE)}
                    for n, t in command.files.items()
                ],
            },
        )
        return command

    def parse_submit(self, result: Any, count: int) -> Any:
        return self._parse("parse_submit", self._inner.parse_submit, result, count)

    def status_command(self, ids: Sequence[str]) -> Any:
        return self._build("status_command", self._inner.status_command, ids)

    def parse_status(self, result: Any, ids: Sequence[str]) -> Any:
        return self._parse("parse_status", self._inner.parse_status, result, ids)

    def history_command(self, ids: Sequence[str]) -> Any:
        return self._build("history_command", self._inner.history_command, ids)

    def parse_history(self, result: Any, ids: Sequence[str]) -> Any:
        return self._parse("parse_history", self._inner.parse_history, result, ids)

    def kill_command(self, ids: Sequence[str]) -> Any:
        return self._build("kill_command", self._inner.kill_command, ids)

    def _build(
        self, call: str, build: Callable[[Sequence[str]], Any], ids: Sequence[str]
    ) -> Any:
        started = time.time()
        argv = build(ids)
        self._tracer.step(
            "scheduler", call, started, {"ids": list(ids), "argv": list(argv)}
        )
        return argv

    def _parse(
        self, call: str, parse: Callable[..., Any], result: Any, extra: Any
    ) -> Any:
        started = time.time()
        try:
            parsed = parse(result, extra)
        except Exception as exc:
            self._tracer.step("scheduler", call, started, _raised(exc))
            raise
        shown = dict(parsed) if isinstance(parsed, Mapping) else list(parsed)
        self._tracer.step("scheduler", call, started, {"parsed": _plain(shown)})
        return parsed


# What of a tool's environment is shown: the configuration the backend gives it,
# never the rest of os.environ, which may hold anything.
_SHOWN_ENVIRONMENT = ("CONDOR_CONFIG", "_CONDOR_")


class TracingRunner:
    """How the HTCondor-CE backend runs each HTCondor tool, with every call seen."""

    def __init__(self, inner: Callable[..., Awaitable[Any]], tracer: Tracer) -> None:
        self._inner = inner
        self._tracer = tracer

    async def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> Any:
        started = time.time()
        detail: dict[str, Any] = {
            "argv": list(argv),
            "env": {k: v for k, v in env.items() if k.startswith(_SHOWN_ENVIRONMENT)},
        }
        if argv and argv[0] == "condor_submit":
            detail["files"] = [
                {"name": Path(arg).name, "text": _cut(Path(arg).read_text(), MAX_FILE)}
                for arg in argv
                if arg.endswith(".sub") and Path(arg).is_file()
            ]
        try:
            result = await self._inner(argv, env, timeout)
        except Exception as exc:
            self._tracer.step("tools", argv[0], started, {**detail, **_raised(exc)})
            raise
        self._tracer.step("tools", argv[0], started, {**detail, **_result(result)})
        return result


class TracingProvider:
    """The consumer's credential provider, with every request a backend makes seen.

    Every credential it hands out is registered with the redactor before it
    leaves, so the value can appear in no event, wherever a tool might print it.
    """

    def __init__(self, inner: Any, tracer: Tracer) -> None:
        self._inner = inner
        self._tracer = tracer

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc, tb)

    async def get(self, requirements: CredentialRequirements) -> Credential:
        started = time.time()
        asked = {
            "resource": requirements.resource,
            "kind": requirements.kind.__name__,
            "scopes": sorted(requirements.scopes),
        }
        try:
            credential = await self._inner.get(requirements)
        except Exception as exc:
            self._tracer.step(
                "provider", "get", started, {"asked": asked, **_raised(exc)}
            )
            raise
        register_credential(self._tracer.bus, credential)
        expires = credential.expires_at.isoformat() if credential.expires_at else None
        self._tracer.step(
            "provider",
            "get",
            started,
            {
                "asked": asked,
                "returned": {
                    "kind": type(credential).__name__,
                    "value": "**********",
                    "expires_at": expires,
                },
            },
        )
        return credential


def register_credential(bus: Bus, credential: Credential) -> None:
    """Make sure ``credential``'s secret material can never be published."""
    if isinstance(credential, BearerToken | SSHPassword):
        bus.redactor.add(credential.value.get_secret_value())
    elif isinstance(credential, SSHPrivateKey):
        bus.redactor.add(credential.pem.get_secret_value())
        if credential.passphrase is not None:
            bus.redactor.add(credential.passphrase.get_secret_value())


def _register_secrets(bus: Bus, copies: Any) -> None:
    if isinstance(copies, int):
        return
    for copy in copies:
        if isinstance(copy, CopyVars):
            for secret in copy.secrets.values():
                bus.redactor.add(secret.get_secret_value())


# -- turning values into something to show ----------------------------------


def _request(
    name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    if name == "submit":
        spec: SubmissionSpec = args[0] if args else kwargs["spec"]
        copies = args[1] if len(args) > 1 else kwargs.get("copies", 1)
        return {
            "spec": spec.model_dump(mode="json", exclude_defaults=True),
            "copies": copies
            if isinstance(copies, int)
            else [
                {
                    "environment": dict(c.environment),
                    "secrets": sorted(c.secrets),
                }  # names only
                for c in copies
            ],
        }
    handles = args[0] if args else kwargs.get("handles", [])
    request: dict[str, Any] = {"handles": [h.key for h in handles]}
    if name == "get_output":
        request["dest"] = str(args[1] if len(args) > 1 else kwargs.get("dest"))
    if name == "get_diagnostics":
        request["filename"] = args[1] if len(args) > 1 else kwargs.get("filename")
    return request


def _outcome(result: Any) -> dict[str, Any]:
    if isinstance(result, Submission):
        return {
            "handles": {str(i): h.key for i, h in result.handles.items()},
            "failures": {str(i): reason for i, reason in result.failures.items()},
        }
    if isinstance(result, Mapping):
        return {"per_job": {_key(k): _plain(v) for k, v in result.items()}}
    return {"value": _plain(result)}


def _key(value: Any) -> str:
    return value.key if isinstance(value, JobHandle) else str(value)


def _plain(value: Any) -> Any:
    if isinstance(value, OpOutcome):
        return {"ok": value.ok, "reason": value.reason, "value": _plain(value.value)}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {_key(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        return value.value  # an enum such as JobStatus
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _result(result: Any) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stdout": _cut(result.stdout, MAX_TEXT),
        "stderr": _cut(result.stderr, MAX_TEXT),
    }


def _raised(exc: Exception) -> dict[str, Any]:
    return {"raised": type(exc).__name__, "message": str(exc)}


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def _listing(local: Path) -> list[dict[str, Any]]:
    """List what a ``put`` copies; show small text files, never a copy's own files.

    A copy's directory holds its secrets, so only the names under it are shown.
    """
    paths = (
        sorted(p for p in local.rglob("*") if p.is_file())
        if local.is_dir()
        else [local]
    )
    files = []
    for path in paths:
        name = str(path.relative_to(local)) if local.is_dir() else path.name
        entry: dict[str, Any] = {"name": name, "bytes": path.stat().st_size}
        if not name.startswith("copies/") and path.stat().st_size <= MAX_FILE:
            try:
                entry["text"] = path.read_text()
            except UnicodeDecodeError:
                pass
        files.append(entry)
    return files
