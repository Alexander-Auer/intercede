"""DRY RUN: the real ``submit()`` of each backend, with only the resource replaced by a recorder.

The translation scene shows what one ``SubmissionSpec`` becomes on each backend.
No pure function in interCEde gives an HTCondor submit description: the
backend's ``submit()`` assembles it. So instead of rebuilding that here, the real
``submit()`` runs, against a stand-in at the lowest seam:

- HTCondor-CE: a runner that reads the description when ``condor_submit`` is
  called, and answers as ``condor_submit -terse`` does;
- the batch backend: a transport that keeps what ``put`` stages and answers
  ``sbatch --parsable`` with an array ID.

Everything shown, including every refusal, is the library's own output. The
job IDs it returns are made up and thrown away; nothing reaches a resource.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from demo.events import Bus
from demo.tracing import (
    Tracer,
    TracingRunner,
    TracingScheduler,
    TracingTransport,
    traced_backend,
)
from intercede import registry
from intercede._backends.batch.protocols import CommandResult
from intercede.auth.models import BearerToken
from intercede.auth.providers import StaticCredentials
from intercede.config import validate_config
from intercede.models import CopyVars, SubmissionSpec

DRY_CLUSTER = 4242
LAUNCHER = ["apptainer", "exec", "--bind", "/cvmfs", "{image}"]

TARGETS = {
    "htcondor-ce": {"kind": "htcondor-ce", "endpoint": "ce.example.org:9619"},
    "slurm": {
        "kind": "batch",
        "transport": "ssh",
        "scheduler": "slurm",
        "host": "login.example.org",
        "user": "dirac",
        "known_hosts": "login.example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDryRunDryRunDryRunDryRunDryRunDryRun",
    },
}


class _RecordingTransport:
    """Stands in for a resource reached through a transport: it records, and runs nothing."""

    resource = "login.example.org"

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
        if argv and argv[0] == "sbatch":
            return CommandResult(returncode=0, stdout=f"{DRY_CLUSTER}\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    async def put(self, local: Path, remote: str, *, timeout: float) -> None:
        return None  # what it would copy is listed by the tracing wrapper around it

    async def get(self, remote: str, local: Path, *, timeout: float) -> None:
        raise NotImplementedError("a dry run collects nothing")


async def _recording_runner(argv: Any, env: Any, timeout: float) -> CommandResult:
    """Answers ``condor_submit -terse`` for the number of jobs the description queues."""
    if argv and argv[0] == "condor_submit":
        description = next(Path(a) for a in argv if a.endswith(".sub"))
        count = int(
            re.findall(r"^queue (\d+)", description.read_text(), re.MULTILINE)[-1]
        )
        return CommandResult(
            returncode=0,
            stdout=f"{DRY_CLUSTER}.0 - {DRY_CLUSTER}.{count - 1}\n",
            stderr="",
        )
    return CommandResult(returncode=0, stdout="", stderr="")


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
    results = {
        name: await _dry(name, config, spec, copies, launcher, tracer)
        for name, config in TARGETS.items()
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
    config: dict[str, Any],
    spec: SubmissionSpec,
    copies: list[CopyVars],
    launcher: bool,
    tracer: Tracer,
) -> dict[str, Any]:
    settings: Any = validate_config(
        {**config, **({"container_launcher": LAUNCHER} if launcher else {})}
    )
    if settings.kind == "batch":
        transport = TracingTransport(_RecordingTransport(), tracer)
        scheduler = TracingScheduler(
            registry._load("scheduler", settings.scheduler).from_settings(settings),
            tracer,
        )
        inner = registry._load("backend", "batch").from_settings(
            settings, transport=transport, scheduler=scheduler
        )
        with_workdir = None
    else:
        with_workdir = tempfile.TemporaryDirectory(prefix="intercede-dryrun-")
        provider = StaticCredentials(
            [BearerToken(value="dry-run-token-not-a-real-one")]
        )
        inner = registry._load("backend", settings.kind)(
            settings,
            provider,
            runner=TracingRunner(_recording_runner, tracer),
            workdir=Path(with_workdir.name),
        )
    backend = traced_backend(inner, f"dry-run:{name}", "DRY RUN", tracer)
    try:
        async with backend:
            submission = await backend.submit(spec, copies)
    except Exception as exc:  # the backend's own refusal, shown as it is
        return {
            "class": type(inner).__name__,
            "raised": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        if with_workdir is not None:
            with_workdir.cleanup()
    operation = tracer.operations[-1]
    return {
        "class": type(inner).__name__,
        "handles": len(submission.handles),
        "steps": [
            s
            for s in operation.steps
            if s["lane"] in ("scheduler", "transport", "tools")
            and s["call"] in ("submit_command", "put", "run", "condor_submit")
        ],
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
