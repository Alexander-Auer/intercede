"""The SSH transport: the resource is a remote host, reached over SSH.

Commands are built on this host and only their argv crosses the connection,
so the remote host needs a POSIX login shell and the batch system's tools, and
no Python. sshd hands every command to the login shell, so the argv is quoted
for a POSIX shell (sh, bash, zsh; not csh). Files move over SFTP, whose paths
no shell ever sees.

One connection, opened at the first call, serves every call until the
transport is closed, and is opened again if it drops. How it logs in is decided
by the configuration and the credential provider alone: nothing on this host
(``~/.ssh``, its config, an agent) is consulted, unless ``auth = "agent"`` asks
for the agent.
"""

from __future__ import annotations

import asyncio
import posixpath
import shlex
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar

import asyncssh

from intercede._backends.batch.protocols import Command, CommandResult
from intercede._backends.credentials import credential_requirements, refresh_if_stale
from intercede.auth.models import CredentialSet, SSHPassword, SSHPrivateKey
from intercede.auth.protocols import CredentialProvider
from intercede.config import BatchSSHConfig
from intercede.exceptions import (
    AuthenticationError,
    InterCEdeError,
    ResourceUnavailableError,
)

# Every command and every copy is a session on the one connection, and
# OpenSSH's sshd allows 10 of them at once by default (MaxSessions).
MAX_SESSIONS = 10

_Channel = TypeVar("_Channel")


class SSHTransport:
    """Runs commands and copies files on a remote host, over one reused connection."""

    def __init__(
        self, settings: BatchSSHConfig, credentials: CredentialProvider
    ) -> None:
        # Construction is cheap and does no I/O.
        self._settings = settings
        self._provider = credentials
        self._resource = f"{settings.user}@{settings.host}:{settings.port}"
        self._credentials: CredentialSet | None = None
        # The jump host first, if there is one; the resource last.
        self._hops: list[asyncssh.SSHClientConnection] = []
        self._connecting = asyncio.Lock()
        self._sessions = asyncio.Semaphore(MAX_SESSIONS)

    @classmethod
    def from_settings(
        cls, settings: BatchSSHConfig, credentials: CredentialProvider
    ) -> Self:
        """Build the transport; how the registry constructs it.

        Args:
            settings: The batch backend's validated configuration: the host,
                the login and the expected host keys.
            credentials: The consumer's provider, asked for a key or password
                when the transport first connects.

        Returns:
            A new transport, not yet connected.
        """
        return cls(settings, credentials)

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self  # it connects at the first call

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Idempotent: closing a transport that is not connected does nothing.
        async with self._connecting:
            await self._disconnect()

    # -- Transport ---------------------------------------------------------

    async def run(self, argv: Command, *, timeout: float) -> CommandResult:
        """Run one command in the home directory; give up after ``timeout`` seconds."""
        with self._errors(f"running {argv[0]}", timeout):
            async with asyncio.timeout(timeout), self._sessions:
                process = await self._open(
                    lambda connection: connection.create_process(
                        shlex.join(argv), stdin=asyncssh.DEVNULL, encoding=None
                    )
                )
                async with process:
                    completed = await process.wait()
        if completed.returncode is None:  # neither an exit status nor a signal
            raise ResourceUnavailableError(
                f"the connection to {self._resource} dropped before {argv[0]} finished"
            )
        return CommandResult(
            returncode=completed.returncode,
            stdout=_text(completed.stdout),
            stderr=_text(completed.stderr),
        )

    async def put(self, local: Path, remote: str, *, timeout: float) -> None:
        """Copy ``local`` to ``remote``, which is relative to the home directory unless absolute."""
        with self._errors(f"copying {local} to {remote}", timeout):
            async with asyncio.timeout(timeout), self._sftp() as sftp:
                # SFTP copies into an existing directory, like scp: only the
                # contents are copied, so they land in ``remote`` itself.
                if local.is_dir() and await sftp.isdir(remote):
                    sources = [str(path) for path in local.iterdir()]
                else:
                    if await sftp.isdir(remote):
                        raise ResourceUnavailableError(
                            f"copying {local} to {remote} failed: it is a directory"
                        )
                    if parent := posixpath.dirname(remote):
                        await sftp.makedirs(parent, exist_ok=True)
                    sources = [str(local)]
                if sources:
                    await sftp.put(sources, remote, recurse=True, preserve=True)

    async def get(self, remote: str, local: Path, *, timeout: float) -> None:
        """Copy ``remote``, relative to the home directory unless absolute, to ``local``."""
        with self._errors(f"copying {remote} to {local}", timeout):
            async with asyncio.timeout(timeout), self._sftp() as sftp:
                # The mirror of put.
                if local.is_dir() and await sftp.isdir(remote):
                    sources = [
                        posixpath.join(remote, name)
                        for name in await sftp.listdir(remote)
                        if name not in (".", "..")
                    ]
                else:
                    if local.is_dir():
                        raise ResourceUnavailableError(
                            f"copying {remote} to {local} failed: it is a directory"
                        )
                    local.parent.mkdir(parents=True, exist_ok=True)
                    sources = [remote]
                if sources:
                    await sftp.get(sources, local, recurse=True, preserve=True)

    # -- the connection ----------------------------------------------------

    async def _open(
        self, start: Callable[[asyncssh.SSHClientConnection], Awaitable[_Channel]]
    ) -> _Channel:
        """Open a session with ``start``, on a new connection if the current one is dead.

        A dropped connection may only show when a session fails to open on it.
        Such a session never carried a command, so opening it again on a new
        connection cannot run anything twice. Whatever was already running when
        a connection dropped is never run again: a second sbatch would submit
        the jobs twice.
        """
        connection = await self._connection()
        try:
            return await start(connection)
        except asyncssh.ChannelOpenError:
            # asyncssh drops a lost connection at once, but reports it closed
            # one turn of the event loop later.
            await asyncio.sleep(0)
            if not connection.is_closed():
                raise  # refused by a live server, e.g. too many sessions
        return await start(await self._connection())

    @asynccontextmanager
    async def _sftp(self) -> AsyncGenerator[asyncssh.SFTPClient]:
        """An SFTP session for one copy, like any other session on the connection."""
        async with self._sessions:
            sftp = await self._open(lambda connection: connection.start_sftp_client())
            async with sftp:
                yield sftp

    async def _connection(self) -> asyncssh.SSHClientConnection:
        """The connection to the resource: opened at the first call, and again after it dropped."""
        async with self._connecting:
            if not self._hops or self._hops[-1].is_closed():
                await self._disconnect()
                self._hops = await self._connect()
            return self._hops[-1]

    async def _connect(self) -> list[asyncssh.SSHClientConnection]:
        credentials = await self._fresh()
        hops: list[asyncssh.SSHClientConnection] = []
        try:
            async with _agent_keys(self._settings.auth == "agent") as agent_keys:
                options = connect_options(
                    self._settings, credentials, agent_keys, self._resource
                )
                for user, host, port in route(self._settings):
                    tunnel = hops[-1] if hops else ()  # () is asyncssh's "none"
                    hops.append(
                        await asyncssh.connect(
                            host, port, username=user, tunnel=tunnel, **options
                        )
                    )
        except BaseException as exc:
            await _close(hops)  # a jump host the resource was not reached through
            if isinstance(exc, TimeoutError):  # asyncssh's connect_timeout
                raise ResourceUnavailableError(
                    f"connecting to {self._resource} took longer than "
                    f"{self._settings.connect_timeout_s} s"
                ) from exc
            raise
        return hops

    async def _disconnect(self) -> None:
        hops, self._hops = self._hops, []
        await _close(hops)

    async def _fresh(self) -> CredentialSet | None:
        requirements = credential_requirements(self._settings)
        if requirements is None:  # the agent holds the key
            return None
        self._credentials = await refresh_if_stale(
            self._credentials, requirements, self._provider
        )
        return self._credentials

    @contextmanager
    def _errors(self, action: str, timeout: float) -> Generator[None, None, None]:
        """Report what asyncssh and the OS raise as interCEde's exceptions."""
        try:
            yield
        except InterCEdeError:
            raise
        except TimeoutError as exc:
            raise ResourceUnavailableError(
                f"{action} on {self._resource} did not finish within {timeout:g} s"
            ) from exc
        except asyncssh.PermissionDenied as exc:
            raise AuthenticationError(
                f"{self._resource} rejected the credentials: {exc.reason}"
            ) from exc
        except asyncssh.HostKeyNotVerifiable as exc:
            raise AuthenticationError(
                f"connecting to {self._resource} failed, its host key does not "
                f"match known_hosts: {exc.reason}"
            ) from exc
        except (asyncssh.Error, OSError) as exc:  # also SFTP and local file errors
            raise ResourceUnavailableError(
                f"{action} on {self._resource} failed: {exc}"
            ) from exc


def connect_options(
    settings: BatchSSHConfig,
    credentials: CredentialSet | None,
    agent_keys: Sequence[asyncssh.SSHKeyPair],
    resource: str,
) -> dict[str, Any]:
    """Say everything about how to log in, so nothing on this host changes it.

    Left to its defaults, asyncssh would read ``~/.ssh/config``, try the keys
    in ``~/.ssh`` and the agent behind ``SSH_AUTH_SOCK``, and attempt Kerberos.

    Args:
        settings: The transport's configuration.
        credentials: What the provider returned; None in agent mode.
        agent_keys: The agent's keys in agent mode; empty otherwise.
        resource: The login, for error messages.

    Returns:
        The keyword arguments for ``asyncssh.connect``, the same for every hop.

    Raises:
        AuthenticationError: The provider's key cannot be read.
    """
    options: dict[str, Any] = {
        "known_hosts": asyncssh.import_known_hosts(settings.known_hosts),
        "config": None,
        "agent_path": None,
        "gss_host": None,
        "host_based_auth": False,
        "connect_timeout": settings.connect_timeout_s,
    }
    if settings.auth == "password":
        password = credentials.get(SSHPassword) if credentials else None
        assert password is not None  # refresh_if_stale checked it is there
        return {
            **options,
            "client_keys": None,  # neither ~/.ssh nor the agent
            "password": password.value.get_secret_value(),
            "preferred_auth": ["keyboard-interactive", "password"],
        }
    if settings.auth == "key":
        key = credentials.get(SSHPrivateKey) if credentials else None
        assert key is not None  # refresh_if_stale checked it is there
        passphrase = key.passphrase.get_secret_value() if key.passphrase else None
        keys: Sequence[asyncssh.SSHKey | asyncssh.SSHKeyPair]
        try:
            keys = [asyncssh.import_private_key(key.pem.get_secret_value(), passphrase)]
        except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError) as exc:
            # Only the type: the message may quote the key.
            raise AuthenticationError(
                f"the SSH key for {resource} cannot be read ({type(exc).__name__}): "
                "is it a private key, with the right passphrase?"
            ) from None
    else:
        keys = agent_keys
    # Never empty: asyncssh reads the keys in ~/.ssh when given none.
    return {**options, "client_keys": keys, "preferred_auth": ["publickey"]}


def route(settings: BatchSSHConfig) -> list[tuple[str, str, int]]:
    """List the logins to make, in order.

    Args:
        settings: The transport's configuration.

    Returns:
        A user, host and port per hop.
    """
    return [(settings.user, settings.host, settings.port)]


@asynccontextmanager
async def _agent_keys(wanted: bool) -> AsyncGenerator[Sequence[asyncssh.SSHKeyPair]]:
    """The agent's keys, usable while connecting; none when the agent is not wanted."""
    if not wanted:
        yield []
        return
    try:
        agent = await asyncssh.connect_agent()
    except (OSError, asyncssh.Error) as exc:
        raise AuthenticationError(
            "auth is 'agent', but no SSH agent can be reached (is SSH_AUTH_SOCK set?)"
        ) from exc
    try:
        keys = await agent.get_keys()
        if not keys:
            raise AuthenticationError(
                "auth is 'agent', but the SSH agent holds no keys"
            )
        yield keys  # the agent signs with them, so it stays open meanwhile
    finally:
        agent.close()
        await agent.wait_closed()


async def _close(hops: Sequence[asyncssh.SSHClientConnection]) -> None:
    for connection in reversed(hops):  # the resource before the host it runs through
        connection.close()
        await connection.wait_closed()


def _text(output: bytes | str | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""
