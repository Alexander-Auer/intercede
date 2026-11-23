"""The demo knows no backend: each comes from interCEde's code and its stacks.

A backend added to interCEde, with its stack in tests/integration/stacks.yml,
must appear on the board and in the dry run without a change to the demo. These
tests hold the demo to that.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from demo import build, doors, dryrun
from demo.columns import Columns
from demo.events import Bus
from demo.tracing import Tracer, traced_backend
from intercede import SubmissionSpec, registry

DEMO = Path(__file__).parents[1]


def _names() -> set[str]:
    """What names a backend: its kind, its scheduler's name, and their classes."""
    available = registry.available()
    composed = {
        kind
        for model in build.composed_models()
        for kind in build.registry_choices(model, "kind")
    }
    kinds = set(available["backend"]) - composed - {"fake"}
    names = kinds | set(available["scheduler"])
    for group, chosen in (
        ("backend", kinds | composed),
        ("scheduler", available["scheduler"]),
    ):
        names |= {registry._load(group, name).__name__ for name in chosen}
    return names


def test_no_backend_is_named_in_the_demo():
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(map(re.escape, sorted(_names()))) + r")(?![\w-])",
        re.IGNORECASE,
    )
    code = [
        path
        for suffix in ("*.py", "*.js", "*.html", "*.css")
        for path in DEMO.rglob(suffix)
        if "tests" not in path.relative_to(DEMO).parts
    ]
    named = [
        f"{path.relative_to(DEMO)}:{number}: {line.strip()}"
        for path in code
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert named == []


def test_every_stack_whose_backend_is_provided_is_a_column():
    columns = Columns(Tracer(Bus())).columns
    expected = {stack for stack in build.stacks() if build.provided(stack)}
    assert set(columns) - {"fake"} == expected
    for stack in expected:
        assert columns[stack].title  # read from the registry or the configuration


def test_every_backend_shape_is_a_dry_run_target():
    shapes = set()
    for stack in build.stacks():
        if build.provided(stack):
            backend = build.static_config(stack)["backend"]  # type: ignore[index]
            shapes.add(backend.get("scheduler") or backend["kind"])
    assert set(dryrun.targets()) == shapes


async def test_a_dry_run_sends_what_it_would_and_reaches_nothing():
    form = {"executable": "/usr/bin/env", "arguments": "true", "copies": 2}
    result = await dryrun.translate(form, launcher=False)
    assert set(result["targets"]) == set(dryrun.targets())
    for name, target in result["targets"].items():
        assert "raised" not in target, (name, target)
        assert any(
            step.get("exchange") or step.get("files") for step in target["steps"]
        ), name
        assert "reaches no resource" not in (target["stopped"] or ""), name


async def test_what_a_backend_cannot_honour_is_refused_before_anything_is_sent():
    form = {"executable": "/usr/bin/env", "container": "docker://almalinux:9"}
    result = await dryrun.translate(form, launcher=False)
    for name, target in result["targets"].items():
        if "raised" in target:
            assert target["raised"] == "SpecificationRejectedError", name


class _Runs:
    """A backend that reaches its resource by running a program."""

    resource = "here"

    async def __aenter__(self) -> _Runs:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def submit(self, spec: object, copies: object = 1) -> str:
        process = await asyncio.create_subprocess_exec(
            "/bin/echo", "hello", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await process.communicate()
        return out.decode()


async def test_a_program_a_backend_runs_is_seen_and_left_alone():
    doors.install()
    tracer = Tracer(Bus())
    traced = traced_backend(_Runs(), "runs", "LIVE", tracer)
    async with traced:
        spec = SubmissionSpec(executable="/bin/echo")
        assert await traced.submit(spec) == "hello\n"  # it ran, as without the demo
    (step,) = tracer.operations[-1].steps
    assert (step["lane"], step["sent"], step["answer"]) == (
        "process",
        "$ /bin/echo hello",
        "exit 0",
    )
    assert step["said"] == "hello\n"


async def test_outside_a_contract_call_nothing_is_recorded():
    doors.install()
    process = await asyncio.create_subprocess_exec("/bin/true")
    assert await process.wait() == 0


@pytest.fixture(autouse=True)
def _installed() -> None:
    doors.install()
