"""Fixtures for the transport suite.

``transport`` is parametrised over every transport the conformance suite runs
against, each built through the registry, so the suite also covers its entry.
The SSH transport logs in to ``sshd``, a server in the test process whose
commands and files live on this host, in the same ``home`` as the local one's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from intercede._backends.batch.protocols import Transport
from intercede.auth import StaticCredentials
from intercede.config import BatchLocalConfig
from intercede.registry import _load

from ._sshd import SSHD


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


@pytest.fixture
async def sshd(home: Path) -> AsyncIterator[SSHD]:
    async with SSHD(home) as server:
        yield server


@pytest.fixture(params=["local", "ssh"])
async def transport(
    request: pytest.FixtureRequest, sshd: SSHD
) -> AsyncIterator[Transport]:
    if request.param == "local":
        config = BatchLocalConfig(kind="batch", transport="local", scheduler="slurm")
        credentials = StaticCredentials()
    else:
        config = sshd.config()
        credentials = StaticCredentials([sshd.key()])
    built = _load("transport", config.transport).from_settings(config, credentials)
    assert isinstance(built, Transport)
    async with built as instance:
        yield instance
