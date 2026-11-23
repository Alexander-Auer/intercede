"""DRY RUN: the real ``submit()`` of each backend, with only the resource replaced by a recorder.

The translation scene shows what one ``SubmissionSpec`` becomes on each backend.
The real ``submit()`` runs, built as the board builds it, and the resource is
replaced where the backend reaches it:

- a composed backend: its transport is a recorder, which runs nothing and keeps
  what it is asked to copy;
- any other backend: the doors it goes through (a program, an HTTP request)
  stand in for the resource (``demo/doors.py``).

The recorder answers every call with nothing, so ``submit()`` stops at the first
answer it needs: by then it has sent what it would send. A refusal comes before
anything is sent. Everything shown is the library's own output; nothing reaches
a resource, and no other connection is allowed while a dry run is going.

The targets come from the stacks' own configurations: each backend once, and
each scheduler of the composed backend once, whatever transport reaches it.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Self

import asyncssh
from pydantic import ValidationError

from demo import build, doors
from demo.events import Bus
from demo.tracing import Tracer
from intercede._backends.batch.protocols import CommandResult
from intercede.auth.models import (
    BearerToken,
    Credential,
    CredentialRequirements,
    SSHPassword,
    SSHPrivateKey,
)
from intercede.config import validate_config
from intercede.exceptions import ConfigurationError, SpecificationRejectedError
from intercede.models import CopyVars, SubmissionSpec

LAUNCHER = ["apptainer", "exec", "--bind", "/cvmfs", "{image}"]


class _RecordingTransport:
    """Stands in for a resource reached through a transport: it records, and runs nothing."""

    resource = "dry-run"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(self, argv: Any, *, timeout: float) -> CommandResult:
        return CommandResult(returncode=0, stdout=doors.STAND_IN_ANSWER, stderr="")

    async def put(self, local: Path, remote: str, *, timeout: float) -> None:
        return None  # what it would copy is listed by the tracing wrapper around it

    async def get(self, remote: str, local: Path, *, timeout: float) -> None:
        raise NotImplementedError("a dry run collects nothing")


class _DryRunProvider:
    """Hands out a made-up credential of whatever kind a backend asks for."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def get(self, requirements: CredentialRequirements) -> Credential:
        if requirements.kind is SSHPrivateKey:
            key = asyncssh.generate_private_key("ssh-ed25519")
            return SSHPrivateKey(pem=key.export_private_key())
        if requirements.kind is SSHPassword:
            return SSHPassword(value="dry-run-password-not-a-real-one")
        return BearerToken(value="dry-run-token-not-a-real-one")


def targets() -> dict[str, tuple[str, dict[str, Any]]]:
    """What the dry run submits to, by name: a title and a [backend] table.

    A composed backend is shown once per scheduler: its transport is replaced,
    so which one a stack uses does not change what is sent.
    """
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for stack_id in build.stacks():
        if not build.provided(stack_id):
            continue
        backend = build.static_config(stack_id)["backend"]  # type: ignore[index]
        model = build.model_of(backend) if _valid(backend) else None
        if model is not None and not build.composed(model):
            found.setdefault(backend["kind"], (build.title(backend), backend))
        elif "scheduler" in backend and backend["scheduler"] not in found:
            settled = _composed_without_its_transport(backend)
            if settled is not None:
                found[backend["scheduler"]] = (
                    f"{backend['scheduler']} (any transport)",
                    settled,
                )
    return found


def _valid(backend: dict[str, Any]) -> bool:
    try:
        validate_config(backend)
    except ConfigurationError:
        return False
    return True


def _composed_without_its_transport(backend: dict[str, Any]) -> dict[str, Any] | None:
    """The table as committed, or as any transport would have it, whichever is complete.

    A stack's table can need what the stack makes at start (an SSH host key); the
    recorder replaces the transport, so any transport's fields will do.
    """
    if _valid(backend):
        return backend
    for model in build.composed_models():
        fields = model.model_fields
        for transport in build.registry_choices(model, "transport"):
            candidate = {
                **{k: v for k, v in backend.items() if k in fields},
                "transport": transport,
            }
            if _valid(candidate):
                return candidate
    return None


async def translate(form: dict[str, Any], launcher: bool) -> dict[str, Any]:
    """Build the spec and the copies from the form, then submit them dry to every target."""
    bus = Bus()
    tracer = Tracer(bus)
    try:
        spec, copies = _spec(form)
    except ValidationError as exc:
        # The models refuse it before any backend sees it.
        return {
            "spec_error": [
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
            ]
        }
    doors.install()
    results = {}
    for name, (title, backend) in targets().items():
        results[name] = {
            "title": title,
            **await _dry(name, backend, spec, copies, launcher, tracer),
        }
    return bus.redactor.scrub(
        {
            "spec": spec.model_dump(mode="json", exclude_defaults=True),
            "targets": results,
        }
    )


def _spec(form: dict[str, Any]) -> tuple[SubmissionSpec, list[CopyVars]]:
    spec = SubmissionSpec.model_validate(
        {
            "executable": form["executable"],
            "arguments": _lines(form.get("arguments", "")),
            "inputs": [
                {"name": n, "source": s} for n, s in _pairs(form.get("inputs", ""), "=")
            ],
            "outputs": {
                "members": [
                    {"pattern": p, "destination": d or None}
                    for p, d in _pairs(form.get("outputs", ""), " ", optional=True)
                ]
            }
            if form.get("outputs", "").strip()
            else None,
            "environment": dict(_pairs(form.get("environment", ""), "=")),
            "resources": {
                k: v
                for k, v in form.get("resources", {}).items()
                if v not in ("", None)
            },
            "container": {"image": form["container"]}
            if form.get("container")
            else None,
        }
    )
    count = max(1, min(int(form.get("copies", 1)), 20))
    secret = form.get("secret") or None
    copies = [
        CopyVars(
            environment={form["copy_variable"]: str(i)}
            if form.get("copy_variable")
            else {},
            secrets={secret: f"per-copy-secret-{i}-{'x' * 16}".encode()}
            if secret
            else {},
        )
        for i in range(count)
    ]
    return spec, copies


async def _dry(
    name: str,
    backend: dict[str, Any],
    spec: SubmissionSpec,
    copies: list[CopyVars],
    launcher: bool,
    tracer: Tracer,
) -> dict[str, Any]:
    if launcher and "container_launcher" in build.model_of(backend).model_fields:  # type: ignore[attr-defined]
        backend = {**backend, "container_launcher": LAUNCHER}
    composed = build.composed(build.model_of(backend))
    traced = build.build(
        backend,
        _DryRunProvider(),
        tracer,
        f"dry-run:{name}",
        "DRY RUN",
        transport=_RecordingTransport() if composed else None,
    )
    shown_class = type(traced.inner).__name__
    with doors.standing_in():
        try:
            async with traced:
                await traced.submit(spec, copies)
        except (SpecificationRejectedError, ConfigurationError) as exc:
            # The backend's own refusal, before anything was sent.
            return {
                "class": shown_class,
                "raised": type(exc).__name__,
                "message": str(exc),
            }
        except (
            Exception
        ) as exc:  # the first answer it needed was the recorder's nothing
            stopped = f"{type(exc).__name__}: {exc}"
        else:
            stopped = None
    operation = tracer.operations[-1]
    return {
        "class": shown_class,
        "stopped": stopped,
        "steps": [s for s in operation.steps if s["lane"] != "provider"],
    }


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _pairs(text: str, separator: str, optional: bool = False) -> list[tuple[str, str]]:
    pairs = []
    for line in _lines(text):
        left, found, right = line.strip().partition(separator)
        if found or optional:
            pairs.append((left.strip(), right.strip()))
    return pairs
