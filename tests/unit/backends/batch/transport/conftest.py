"""Fixtures for the transport suite.

``transport`` is parametrised over every transport the conformance suite runs
against, each built through the registry, so the suite also covers its entry.
Only the local transport exists for now.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from intercede._backends.batch.protocols import Transport
from intercede.auth import StaticCredentials
from intercede.config import BatchLocalConfig, BatchSSHConfig
from intercede.registry import _load

CONFIGS: dict[str, BatchLocalConfig | BatchSSHConfig] = {
    "local": BatchLocalConfig(kind="batch", transport="local", scheduler="slurm"),
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The account's home directory on the resource; a private one, never the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def remote(home: Path) -> str:
    """An empty directory on the resource, as the absolute path a backend would use."""
    directory = home / "work"
    directory.mkdir()
    return str(directory)


@pytest.fixture
def here(tmp_path: Path) -> Path:
    """An empty directory on this host."""
    directory = tmp_path / "here"
    directory.mkdir()
    return directory


@pytest.fixture(params=sorted(CONFIGS))
async def transport(
    request: pytest.FixtureRequest, home: Path
) -> AsyncIterator[Transport]:
    config = CONFIGS[request.param]
    built = _load("transport", config.transport).from_settings(
        config, StaticCredentials()
    )
    assert isinstance(built, Transport)
    async with built as instance:
        yield instance
