"""The backends on the board: how each is built, traced, and found to be online.

A live column is built from its stack's own ``intercede-client.toml`` and the
credentials the stack minted at start, read the way the integration suite reads
them. The classes come from the registry's own tables, so the demo runs the same
code a consumer gets from ``registry.get_backend``; only the seams are traced:

- ``batch``: the registry's transport and scheduler, each wrapped, composed by
  ``BatchBackend.from_settings`` exactly as ``registry._build`` composes them;
- ``htcondor-ce``: the registry's backend class with a tracing ``runner``, the
  callable every HTCondor tool runs through;
- every backend: the consumer's provider, and the backend itself, are wrapped.
"""

from __future__ import annotations

import asyncio
import tomllib
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.integration.conftest import _READERS
from tests.integration.generate_matrix import STACKS_DIR, compose_env, load_manifest

from demo.agent import AgentBackend
from demo.tracing import (
    Tracer,
    TracingProvider,
    TracingRunner,
    TracingScheduler,
    TracingTransport,
    capabilities,
    traced_backend,
)
from intercede import registry
from intercede._backends.batch.protocols import Scheduler, Transport
from intercede._backends.fake import FakeBackend
from intercede._backends.monolith.htcondorce.process import run_process
from intercede.auth.providers import StaticCredentials
from intercede.config import validate_config

# The lanes of each kind's sequence diagram, top to bottom of the stack.
LANES = {
    "fake": ["consumer", "backend", "resource"],
    "monolith": ["consumer", "backend", "provider", "tools", "resource"],
    "batch": ["consumer", "backend", "provider", "scheduler", "transport", "resource"],
}


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
            "backend_class": type(self.backend.inner).__name__
            if self.backend
            else None,
            "resource": self.backend.resource if self.backend else None,
            "lanes": LANES[self.kind],
            "start": start_command(self.stack) if self.stack else None,
        }


def stacks() -> dict[str, dict[str, Any]]:
    """The entries of ``tests/integration/stacks.yml``, by id."""
    return {entry["id"]: entry for entry in load_manifest()}


def compose_file(stack_id: str) -> Path:
    return STACKS_DIR / compose_env(stacks()[stack_id]) / "compose.yml"


def start_command(stack_id: str) -> str:
    """The command that starts a stack, as docs/dev/how-to/run-integration-tests-locally.md gives it."""
    return (
        f"docker compose -f {compose_file(stack_id).relative_to(Path.cwd())} up --wait"
    )


def client_config(stack_id: str) -> dict[str, Any]:
    """Read a stack's ``basic`` client configuration, as the integration suite does.

    The host key an SSH stack creates when it starts is added to its [backend].
    """
    path = STACKS_DIR / stack_id / "config" / "basic" / "intercede-client.toml"
    with path.open("rb") as f:
        config = tomllib.load(f)
    if config.get("credentials", {}).get("type") == "ssh-key":
        known_hosts = (
            compose_file(stack_id).parent
            / config["credentials"]["path"]
            / "known_hosts"
        )
        config["backend"]["known_hosts"] = known_hosts.read_text()
    return config


def credential(stack_id: str, config: dict[str, Any]) -> Any:
    section = config["credentials"]
    return _READERS[section["type"]](compose_file(stack_id).parent / section["path"])


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
            "htcondor": Column(
                "htcondor", "HTCondor-CE", "monolith", "LIVE", stack="htcondor"
            ),
            "ssh-slurm": Column(
                "ssh-slurm", "SSH + Slurm", "batch", "LIVE", stack="ssh-slurm"
            ),
            "local-slurm": Column(
                "local-slurm", "local + Slurm", "batch", "LIVE", stack="local-slurm"
            ),
        }

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
            # Its local transport must run where Slurm is: in the container (demo/agent.py).
            agent = AgentBackend(compose_file(column.stack), self.tracer)
            return traced_backend(agent, column.id, column.source, self.tracer)
        config = client_config(column.stack)
        settings = validate_config(config["backend"])
        held = [credential(column.stack, config)] if "credentials" in config else []
        # The consumer opens its provider once and shares it; interCEde never closes it.
        provider = await exits.enter_async_context(
            TracingProvider(StaticCredentials(held), self.tracer)
        )

        if settings.kind == "batch":
            transport = TracingTransport(
                registry._load("transport", settings.transport).from_settings(
                    settings, provider
                ),
                self.tracer,
            )
            scheduler = TracingScheduler(
                registry._load("scheduler", settings.scheduler).from_settings(settings),
                self.tracer,
            )
            # The same checks the registry makes before composing them.
            registry._checked("transport", settings.transport, transport, Transport)
            registry._checked("scheduler", settings.scheduler, scheduler, Scheduler)
            inner = registry._load("backend", "batch").from_settings(
                settings, transport=transport, scheduler=scheduler
            )
        else:
            inner = registry._load("backend", settings.kind)(
                settings, provider, runner=TracingRunner(run_process, self.tracer)
            )
        return traced_backend(inner, column.id, column.source, self.tracer)

    async def _problem(self, column: Column) -> str | None:
        """Say why a live column cannot be used now, or None when it can."""
        assert column.stack is not None
        try:
            config = client_config(column.stack)
        except FileNotFoundError as exc:
            return f"{Path(exc.filename).name} is missing: the stack is not running"
        backend = config["backend"]
        if stacks()[column.stack].get("exec_in_container"):
            if not await container_running(compose_file(column.stack)):
                return "its container is not running"
            return None
        if "endpoint" in backend:
            host, _, port = backend["endpoint"].partition(":")
        else:
            host, port = backend["host"], str(backend.get("port", 22))
        if not await reachable(host, int(port or 9619)):
            return f"nothing answers on {host}:{port}"
        if "credentials" in config:
            try:
                credential(column.stack, config)
            except FileNotFoundError as exc:
                return f"{Path(exc.filename).name} is missing: the stack is not running"
        return None
