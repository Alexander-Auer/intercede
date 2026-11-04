"""N + M: every transport works with every scheduler, and neither knows the other."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import intercede
from intercede import registry
from intercede.auth import StaticCredentials
from intercede.exceptions import ConfigurationError
from intercede.models import JobStatus, SubmissionSpec
from intercede.protocols import JobBackend, Killable, OutputRetriever, Purgeable

from ._fake_slurm import FakeSlurm
from .conftest import config_of
from .transport._sshd import SSHD

SOURCE = Path(intercede.__file__).parent / "_backends"
BATCH = SOURCE / "batch"
DIRECT = "tests.unit.backends.batch._direct_scheduler:Direct"

SHIPPED = registry.available()
COMBINATIONS = [
    (transport, scheduler)
    for transport in SHIPPED["transport"]
    for scheduler in SHIPPED["scheduler"]
]


def imported_by(path: Path) -> Iterator[str]:
    """Yield every module a source file imports."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def sources(*places: Path) -> list[Path]:
    return [
        path
        for place in places
        for path in ([place] if place.is_file() else place.rglob("*.py"))
    ]


@pytest.fixture
def register_direct(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Register a test-only class as the ``direct`` scheduler, for this test only."""

    def register(target: str) -> None:
        monkeypatch.setitem(registry._SCHEDULERS, "direct", target)

    registry._load.cache_clear()  # it remembers what a name was
    yield register
    registry._load.cache_clear()


class TestNoPartKnowsAnother:
    @pytest.mark.parametrize("path", sources(*BATCH.glob("*.py")), ids=lambda p: p.name)
    def test_the_backend_knows_no_transport_or_scheduler(self, path: Path):
        forbidden = ("asyncssh", "intercede._backends._slurm")
        forbidden += ("intercede._backends.batch.transport",)
        forbidden += ("intercede._backends.batch.scheduler",)
        assert not [
            module for module in imported_by(path) if module.startswith(forbidden)
        ]

    @pytest.mark.parametrize(
        "path",
        sources(BATCH / "scheduler", SOURCE / "_slurm"),
        ids=lambda p: p.name,
    )
    def test_a_scheduler_knows_no_transport(self, path: Path):
        forbidden = ("asyncssh", "intercede._backends.batch.transport")
        assert not [
            module for module in imported_by(path) if module.startswith(forbidden)
        ]

    @pytest.mark.parametrize("path", sources(BATCH / "transport"), ids=lambda p: p.name)
    def test_a_transport_knows_no_scheduler(self, path: Path):
        forbidden = (
            "intercede._backends._slurm",
            "intercede._backends.batch.scheduler",
        )
        assert not [
            module for module in imported_by(path) if module.startswith(forbidden)
        ]


class TestEveryCombination:
    @pytest.mark.parametrize(("transport", "scheduler"), COMBINATIONS)
    async def test_what_ships_is_built_and_runs_a_job(
        self,
        transport: str,
        scheduler: str,
        sshd: SSHD,
        slurm: FakeSlurm,
        job_script: Callable[..., str],
    ):
        await round_trip(transport, scheduler, sshd, job_script)

    @pytest.mark.parametrize("transport", SHIPPED["transport"])
    async def test_a_scheduler_with_nothing_of_slurm_works_the_same(
        self,
        transport: str,
        sshd: SSHD,
        home: Path,
        register_direct: Callable[[str], None],
        job_script: Callable[..., str],
    ):
        register_direct(DIRECT)
        await round_trip(transport, "direct", sshd, job_script)


async def round_trip(
    transport: str, scheduler: str, sshd: SSHD, job_script: Callable[..., str]
) -> None:
    config = config_of(transport, sshd, scheduler=scheduler)
    built = registry.get_backend(config, StaticCredentials([sshd.key()]))
    assert isinstance(built, JobBackend)
    async with built as backend:
        ok = await backend.submit(SubmissionSpec(executable=job_script("true")))
        failed = await backend.submit(SubmissionSpec(executable=job_script("exit 3")))
        statuses = await backend.get_status([ok.handles[0], failed.handles[0]])
    assert list(statuses.values()) == [JobStatus.DONE, JobStatus.FAILED]


class TestTheBackend:
    def test_it_has_the_contracts_capabilities(self, sshd: SSHD):
        built = registry.get_backend(config_of("local", sshd), StaticCredentials())
        assert isinstance(built, JobBackend)
        assert isinstance(built, OutputRetriever)
        assert isinstance(built, Killable)
        assert isinstance(built, Purgeable)

    def test_building_needs_neither_tools_nor_credentials(self, sshd: SSHD):
        built: Any = registry.get_backend(config_of("ssh", sshd), StaticCredentials())
        assert built.resource == f"dirac@127.0.0.1:{sshd.port}"
        assert sshd.logins == 0

    def test_it_refuses_a_scheduler_that_moves_files_itself(
        self, sshd: SSHD, register_direct: Callable[[str], None]
    ):
        register_direct(f"{DIRECT}MovingFiles")
        config = config_of("local", sshd, scheduler="direct")
        with pytest.raises(ConfigurationError, match="moves a job's files itself"):
            registry.get_backend(config, StaticCredentials())
