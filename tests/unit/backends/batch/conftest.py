"""Fixtures for the composed backend's tests.

``backend`` is built by the registry from a configuration, over every transport:
each test runs through the public interface, with ``sbatch`` and the others on
``PATH``, whichever way they are reached.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from intercede import registry
from intercede.auth import StaticCredentials
from intercede.config import BatchLocalConfig, BatchSSHConfig

from . import _fake_slurm
from .transport._sshd import SSHD


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The account's home directory on the resource; a private one, never the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
async def sshd(home: Path) -> AsyncIterator[SSHD]:
    async with SSHD(home) as server:
        yield server


@pytest.fixture
def slurm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _fake_slurm.FakeSlurm:
    """Slurm's tools on ``PATH``, for this test only."""
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in _fake_slurm.TOOLS:
        tool = tools / name
        tool.write_text(f"#!{sys.executable}\n{Path(_fake_slurm.__file__).read_text()}")
        tool.chmod(0o755)
    state = tmp_path / "slurm"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(_fake_slurm.STATE_VARIABLE, str(state))
    return _fake_slurm.FakeSlurm(state)


def config_of(
    transport: str, sshd: SSHD, **fields: Any
) -> BatchLocalConfig | BatchSSHConfig:
    """A configuration of the composed backend over ``transport``."""
    if transport == "ssh":
        return sshd.config(**fields)
    defaults = {"kind": "batch", "transport": "local", "scheduler": "slurm"}
    return BatchLocalConfig(**{**defaults, **fields})


@pytest.fixture(params=["local", "ssh"])
def transport_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def credentials(sshd: SSHD) -> StaticCredentials:
    return StaticCredentials([sshd.key()])


@pytest.fixture
def backend_with(
    transport_name: str,
    sshd: SSHD,
    slurm: _fake_slurm.FakeSlurm,
    credentials: StaticCredentials,
) -> Callable[..., Any]:
    """Build the composed backend over each transport, on Slurm, with these settings."""

    def build(**fields: Any) -> Any:
        config = config_of(transport_name, sshd, **fields)
        return registry.get_backend(config, credentials)

    return build


@pytest.fixture
async def backend(backend_with: Callable[..., Any]) -> AsyncIterator[Any]:
    """The composed backend over each transport, on Slurm, open."""
    async with backend_with() as opened:
        yield opened


@pytest.fixture
def job_script(tmp_path: Path) -> Any:
    """Write a job script on this host and return its path."""

    def write(body: str = "true") -> str:
        path = tmp_path / "job.sh"
        path.write_text(f"#!/bin/sh\n{body}\n")
        return str(path)

    return write
