"""The backends on the board: which there are, and when each is online.

There is a column per stack in ``tests/integration/stacks.yml`` whose backend
this version provides, built from the stack's own ``intercede-client.toml`` and
the credentials the stack minted at start (``demo/build.py``), plus the Fake,
which needs nothing. A backend added to interCEde, with its stack, appears here
without a change to the demo.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from demo import build
from demo.agent import AgentBackend
from demo.build import client_config, compose_file, credential, stacks
from demo.tracing import Tracer, TracingProvider, capabilities, traced_backend
from intercede._backends.fake import FakeBackend
from intercede.auth.providers import StaticCredentials

# The lanes of a sequence diagram, top to bottom of the stack. Below the
# provider, the parts a composed backend is made of; any other backend adds the
# doors it went through (a process, HTTP) as its calls use them.
LANES = {
    "fake": ["consumer", "backend", "resource"],
    "composed": [
        "consumer",
        "backend",
        "provider",
        "scheduler",
        "transport",
        "resource",
    ],
}
DEFAULT_LANES = ["consumer", "backend", "provider", "resource"]


@dataclass
class Column:
    """One backend on the board."""

    id: str
    title: str
    kind: str
    source: str
    stack: str | None = None
    state: str = "offline"
    reason: str = ""
    backend: Any = None
    fake: FakeBackend | None = None  # the Fake column's own backend, for its test hooks
    exits: AsyncExitStack | None = None
    caps: list[str] = field(default_factory=list)

    def as_event(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "source": self.source,
            "stack": self.stack,
            "state": self.state,
            "reason": self.reason,
            "capabilities": self.caps,
            # The agent's proxy says what runs in the container behind it.
            "backend_class": getattr(
                self.backend.inner, "shown_class", type(self.backend.inner).__name__
            )
            if self.backend
            else None,
            "resource": self.backend.resource if self.backend else None,
            "lanes": LANES.get(self.kind, DEFAULT_LANES),
            "start": start_command(self.stack) if self.stack else None,
        }


def start_command(stack_id: str) -> str:
    """The command that starts a stack, as docs/dev/how-to/run-integration-tests-locally.md gives it."""
    return (
        f"docker compose -f {compose_file(stack_id).relative_to(Path.cwd())} up --wait"
    )


async def container_running(compose: Path) -> bool:
    """Tell whether a stack's ``backend`` service is running."""
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-f",
        str(compose),
        "ps",
        "--status",
        "running",
        "--services",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    return "backend" in out.decode().split()


async def reachable(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 1)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


class Columns:
    """Builds every column, and rebuilds a live one whenever its stack comes back."""

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer
        self.columns: dict[str, Column] = {
            "fake": Column("fake", "Fake", "fake", "FAKE"),
        }
        for stack_id in stacks():
            if not build.provided(stack_id):
                continue  # its backend has not landed yet
            backend = build.static_config(stack_id)["backend"]  # type: ignore[index]
            self.columns[stack_id] = Column(
                stack_id,
                build.title(backend),
                build.family(build.model_of(backend)),
                "LIVE",
                stack=stack_id,
            )

    def snapshot(self) -> list[dict[str, Any]]:
        return [column.as_event() for column in self.columns.values()]

    def publish(self) -> None:
        self.tracer.bus.publish({"type": "columns", "columns": self.snapshot()})

    async def start(self) -> None:
        await self._open(self.columns["fake"], self._build_fake)
        await self.refresh()

    async def refresh(self) -> None:
        """Bring each live column in line with whether its stack is reachable now."""
        changed = False
        for column in self.columns.values():
            if column.stack is None:
                continue
            problem = await self._problem(column)
            if problem is None and column.state != "online":
                await self._open(column, self._build_live)
                changed = True
            elif problem is not None and column.state == "online":
                await self._close(column)
                column.state, column.reason = "offline", problem
                changed = True
            elif problem is not None and column.reason != problem:
                column.state, column.reason = "offline", problem
                changed = True
        if changed:
            self.publish()

    async def rebuild(self, column: Column) -> None:
        """Close a column's backend and build a fresh one from configuration."""
        await self._close(column)
        build = self._build_fake if column.kind == "fake" else self._build_live
        await self._open(column, build, keep_fake=True)
        self.publish()

    async def close(self) -> None:
        for column in self.columns.values():
            await self._close(column)

    # -- building ------------------------------------------------------------

    async def _open(self, column: Column, build: Any, keep_fake: bool = False) -> None:
        exits = AsyncExitStack()
        try:
            backend = await build(column, exits, keep_fake)
            column.backend = await exits.enter_async_context(backend)
        except (
            Exception
        ) as exc:  # shown on the board: a column must never take the demo down
            await exits.aclose()
            column.state, column.reason, column.backend = (
                "offline",
                f"{type(exc).__name__}: {exc}",
                None,
            )
            return
        column.exits = exits
        column.state, column.reason = "online", ""
        column.caps = capabilities(column.backend)

    async def _close(self, column: Column) -> None:
        if column.exits is not None:
            exits, column.exits = column.exits, None
            try:
                await exits.aclose()
            except Exception:  # noqa: S110  (closing a backend whose resource is gone)
                pass
        column.backend = None
        column.state = "offline"

    async def _build_fake(
        self, column: Column, exits: AsyncExitStack, keep_fake: bool
    ) -> Any:
        # The Fake is the resource as well as the backend: a restart closes and
        # reopens the same object, so its jobs survive, as a real CE's queue does.
        if not (keep_fake and column.fake is not None):
            column.fake = FakeBackend(resource="fake://demo")
        return traced_backend(column.fake, column.id, column.source, self.tracer)

    async def _build_live(
        self, column: Column, exits: AsyncExitStack, keep_fake: bool
    ) -> Any:
        assert column.stack is not None
        if stacks()[column.stack].get("exec_in_container"):
            # It runs where the integration suite runs it: in the container (demo/agent.py).
            agent = AgentBackend(
                compose_file(column.stack), column.stack, column.id, self.tracer
            )
            return traced_backend(agent, column.id, column.source, self.tracer)
        config = client_config(column.stack)
        build.trust(column.stack, config)
        held = [credential(column.stack, config)] if "credentials" in config else []
        # The consumer opens its provider once and shares it; interCEde never closes it.
        provider = await exits.enter_async_context(
            TracingProvider(StaticCredentials(held), self.tracer)
        )
        return build.build(
            config["backend"], provider, self.tracer, column.id, column.source
        )

    async def _problem(self, column: Column) -> str | None:
        """Say why a live column cannot be used now, or None when it can."""
        assert column.stack is not None
        try:
            config = client_config(column.stack)
        except FileNotFoundError as exc:
            return f"{Path(exc.filename).name} is missing: the stack is not running"
        if not await container_running(compose_file(column.stack)):
            return "its container is not running"
        if stacks()[column.stack].get("exec_in_container"):
            return None  # reached from inside its container, not over the network
        address = _address(config["backend"])
        if address is not None and not await reachable(*address):
            return f"nothing answers on {address[0]}:{address[1]}"
        if "credentials" in config:
            try:
                credential(column.stack, config)
            except FileNotFoundError as exc:
                return f"{Path(exc.filename).name} is missing: the stack is not running"
        return None


def _address(backend: dict[str, Any]) -> tuple[str, int] | None:
    """Where a configuration says its resource listens, when it says so.

    An ``endpoint`` is a URL or ``host:port``; otherwise a ``host`` and its
    ``port``. Without a port it gives, there is nothing to check.
    """
    if "endpoint" in backend:
        endpoint = str(backend["endpoint"])
        url = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
        port = url.port or {"https": 443, "http": 80}.get(url.scheme)
        return (url.hostname, port) if url.hostname and port else None
    if "host" in backend and "port" in backend:
        return str(backend["host"]), int(backend["port"])
    return None
