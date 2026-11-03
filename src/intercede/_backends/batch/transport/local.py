"""The local transport: the resource is this host, under the account interCEde runs as.

Every command is its own subprocess, started directly, never through a shell,
and every copy is an ordinary file copy. It needs no credentials and holds no
connection, so it serves batch systems whose client tools are installed here.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import TracebackType
from typing import Self

from intercede._backends.batch.protocols import Command, CommandResult
from intercede.auth.protocols import CredentialProvider
from intercede.config import BatchLocalConfig
from intercede.exceptions import ResourceUnavailableError


class LocalTransport:
    """Runs commands and copies files on this host."""

    @classmethod
    def from_settings(
        cls, settings: BatchLocalConfig, credentials: CredentialProvider
    ) -> Self:
        """Build the transport; how the registry constructs it.

        Args:
            settings: The batch backend's validated configuration. Nothing in
                it concerns this transport.
            credentials: The consumer's provider, never asked: running on this
                host needs no credentials.

        Returns:
            A new transport.
        """
        return cls()

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Nothing to close: every command is its own process.
        pass

    # -- Transport ---------------------------------------------------------

    async def run(self, argv: Command, *, timeout: float) -> CommandResult:
        """Run one command in the home directory and wait for it, killing it after ``timeout`` seconds."""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=Path.home(),
            )
        # Reported as a shell would, so the result is the same over SSH.
        except FileNotFoundError:
            return _not_started(127, f"{argv[0]}: command not found")
        except PermissionError:
            return _not_started(126, f"{argv[0]}: Permission denied")
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError as exc:
            raise ResourceUnavailableError(
                f"{argv[0]} did not finish within {timeout:g} s"
            ) from exc
        finally:
            if process.returncode is None:  # timed out, or the caller was cancelled
                process.kill()
                await process.wait()
        return CommandResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def put(self, local: Path, remote: str, *, timeout: float) -> None:
        """Copy ``local`` to ``remote``, which is relative to the home directory unless absolute."""
        await _copy(local, Path.home() / remote, timeout)

    async def get(self, remote: str, local: Path, *, timeout: float) -> None:
        """Copy ``remote``, relative to the home directory unless absolute, to ``local``."""
        await _copy(Path.home() / remote, local, timeout)


def _not_started(returncode: int, message: str) -> CommandResult:
    return CommandResult(returncode=returncode, stdout="", stderr=f"{message}\n")


async def _copy(source: Path, destination: Path, timeout: float) -> None:
    try:
        # A thread cannot be stopped: after a timeout the copy runs to its end
        # in the background, but the caller is no longer kept waiting.
        await asyncio.wait_for(asyncio.to_thread(copy, source, destination), timeout)
    except TimeoutError as exc:
        raise ResourceUnavailableError(
            f"copying {source} to {destination} did not finish within {timeout:g} s"
        ) from exc
    except OSError as exc:  # also shutil.Error, which collects a tree's failures
        raise ResourceUnavailableError(
            f"copying {source} to {destination} failed: {exc}"
        ) from exc


def copy(source: Path, destination: Path) -> None:
    """Copy a file or directory so that ``destination`` is the copy, permission bits included."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        # Not copy2: given an existing directory, it would copy into it.
        shutil.copyfile(source, destination)
        shutil.copymode(source, destination)
