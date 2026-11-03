"""An SSH server inside the test process, for the SSH transport to log in to.

It does with what the transport sends what OpenSSH's sshd does: a command goes
to ``sh -c`` in the home directory, and SFTP paths are relative to that home.
It also counts what happens, so the SSH-only tests can check connection reuse,
the session limit and the jump host.
"""

from __future__ import annotations

import asyncio
import posixpath
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import asyncssh

from intercede.auth import SSHPassword, SSHPrivateKey
from intercede.config import BatchSSHConfig

HOST = "127.0.0.1"
USER = "dirac"
PASSWORD = "correct horse battery staple"  # noqa: S105  (the test server's)


class SSHD:
    """One test SSH server; ``async with`` starts and stops it."""

    def __init__(self, home: Path, client_key: asyncssh.SSHKey | None = None) -> None:
        self.home = home
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        # The key it accepts; a jump host shares the resource's.
        self.client_key = client_key or asyncssh.generate_private_key("ssh-ed25519")
        self.logins = 0
        self.commands: list[str] = []
        self.started = asyncio.Event()  # set when a command starts
        self.most_at_once = 0
        self.forwarded = 0  # connections passed on to another host, as a jump host
        self._running = 0
        self._connections: set[asyncssh.SSHServerConnection] = set()
        self._children: set[asyncio.subprocess.Process] = set()
        self._acceptor: asyncssh.SSHAcceptor | None = None

    async def __aenter__(self) -> Self:
        public = self.client_key.export_public_key().decode()
        self._acceptor = await asyncssh.create_server(
            lambda: _Login(self),
            HOST,
            0,
            server_host_keys=[self.host_key],
            authorized_client_keys=asyncssh.import_authorized_keys(public),
            process_factory=self._run,
            sftp_factory=lambda channel: _HomeSFTP(channel, self.home),
            encoding=None,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for child in self._children:
            if child.returncode is None:
                child.kill()
        await self.drop_connections()
        assert self._acceptor is not None
        self._acceptor.close()
        await self._acceptor.wait_closed()

    @property
    def port(self) -> int:
        assert self._acceptor is not None
        return self._acceptor.get_port()

    @property
    def connected(self) -> int:
        """How many clients are connected right now."""
        return len(self._connections)

    def known_hosts(self, host_key: asyncssh.SSHKey | None = None) -> str:
        """The line a client trusts this server with; another key's to test a mismatch."""
        key = (host_key or self.host_key).export_public_key().decode().strip()
        return f"[{HOST}]:{self.port} {key}"

    def config(self, **fields: Any) -> BatchSSHConfig:
        """A configuration that logs in to this server with its authorized key."""
        defaults: dict[str, Any] = {
            "kind": "batch",
            "transport": "ssh",
            "scheduler": "slurm",
            "host": HOST,
            "port": self.port,
            "user": USER,
            "known_hosts": self.known_hosts(),
        }
        return BatchSSHConfig(**{**defaults, **fields})

    def key(self, passphrase: str | None = None) -> SSHPrivateKey:
        """The authorized key, as a credential provider hands it over."""
        if passphrase is None:
            return SSHPrivateKey(pem=self.client_key.export_private_key())
        pem = self.client_key.export_private_key("pkcs8-pem", passphrase)
        return SSHPrivateKey(pem=pem, passphrase=passphrase)

    @staticmethod
    def password(value: str = PASSWORD) -> SSHPassword:
        return SSHPassword(value=value)

    async def drop_connections(self) -> None:
        """Close every connection from the server's side, as a restarted sshd would."""
        for connection in list(self._connections):
            connection.close()
            await connection.wait_closed()

    async def _run(self, process: asyncssh.SSHServerProcess[bytes]) -> None:
        command = process.command or ""
        self.commands.append(command)
        self._running += 1
        self.most_at_once = max(self.most_at_once, self._running)
        self.started.set()
        try:
            stdin = await process.stdin.read()  # until the client sends EOF
            child = await asyncio.create_subprocess_exec(
                "sh",
                "-c",
                command,
                cwd=self.home,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._children.add(child)
            stdout, stderr = await child.communicate(stdin)
            process.stdout.write(stdout)
            process.stderr.write(stderr)
            process.exit(child.returncode if child.returncode is not None else -1)
        except (asyncssh.Error, OSError):
            pass  # the client went away first, e.g. after its timeout
        finally:
            self._running -= 1


class _Login(asyncssh.SSHServer):
    def __init__(self, sshd: SSHD) -> None:
        self._sshd = sshd
        self._connection: asyncssh.SSHServerConnection | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._connection = conn
        self._sshd._connections.add(conn)

    def connection_lost(self, exc: Exception | None) -> None:
        self._sshd._connections.discard(self._connection)  # type: ignore[arg-type]

    def begin_auth(self, username: str) -> bool:
        return True  # every login authenticates

    def auth_completed(self) -> None:
        self._sshd.logins += 1

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USER and password == PASSWORD

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> bool:
        self._sshd.forwarded += 1
        return True  # as a jump host, pass the connection on


class _HomeSFTP(asyncssh.SFTPServer):
    """SFTP whose relative paths are relative to the home directory, as with sshd."""

    def __init__(self, channel: asyncssh.SSHServerChannel[bytes], home: Path) -> None:
        super().__init__(channel)
        self._home = bytes(home)

    def map_path(self, path: bytes) -> bytes:
        return posixpath.join(self._home, path)  # an absolute path stays as it is
