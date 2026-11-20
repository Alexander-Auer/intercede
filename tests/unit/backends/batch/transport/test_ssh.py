"""What only the SSH transport does, and a stack does not show.

What every transport does is in ``test_conformance.py``, which runs over SSH too,
and on every stack reached over SSH; logging in with a key, a rejected key and a
host that is down are the shared suite's. These tests are for failures and kinds of
login a stack does not produce, against an SSH server in the test process.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from intercede._backends.batch.transport.ssh import SSHTransport
from intercede.auth import (
    Credential,
    SSHPassword,
    SSHPrivateKey,
    StaticCredentials,
)
from intercede.config import BatchSSHConfig
from intercede.exceptions import AuthenticationError, ResourceUnavailableError

from ._sshd import SSHD

TIMEOUT = 30  # seconds
OPENSSH_MAX_SESSIONS = 10  # sshd's default; the transport must stay within it
PASSPHRASE = "open sesame"  # noqa: S105  (the test key's)
WRONG = "hunter2"  # noqa: S105  (neither the password nor the passphrase)


def transport(config: BatchSSHConfig, *credentials: Credential) -> SSHTransport:
    return SSHTransport(config, StaticCredentials(credentials))


async def echo(ssh: SSHTransport, text: str = "ok") -> str:
    result = await ssh.run(["echo", text], timeout=TIMEOUT)
    assert result.returncode == 0, result
    return result.stdout


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait for something the server notices a moment after the client acts."""
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        await asyncio.sleep(0.01)


@pytest.fixture
async def ssh(sshd: SSHD) -> AsyncIterator[SSHTransport]:
    async with transport(sshd.config(), sshd.key()) as instance:
        yield instance


class TestConnection:
    async def test_commands_beyond_the_session_limit_wait_their_turn(
        self, ssh: SSHTransport, sshd: SSHD
    ):
        await asyncio.gather(
            *(
                ssh.run(["sleep", "0.5"], timeout=TIMEOUT)
                for _ in range(2 * OPENSSH_MAX_SESSIONS)
            )
        )
        assert 1 < sshd.most_at_once <= OPENSSH_MAX_SESSIONS

    async def test_a_dropped_connection_is_opened_again(
        self, ssh: SSHTransport, sshd: SSHD
    ):
        await echo(ssh)
        await sshd.drop_connections()
        assert await echo(ssh) == "ok\n"
        assert sshd.logins == 2

    async def test_a_command_cut_off_is_not_run_again(
        self, ssh: SSHTransport, sshd: SSHD
    ):
        cut_off = asyncio.create_task(ssh.run(["sleep", "5"], timeout=TIMEOUT))
        await sshd.started.wait()
        await sshd.drop_connections()
        with pytest.raises(ResourceUnavailableError):
            await cut_off
        await echo(ssh)  # the next call connects again
        assert [c for c in sshd.commands if "sleep" in c] == ["sleep 5"]

    async def test_a_command_that_outlasts_its_timeout_is_killed(
        self, ssh: SSHTransport, sshd: SSHD
    ):
        with pytest.raises(ResourceUnavailableError, match="did not finish"):
            await ssh.run(["sleep", "30"], timeout=0.2)
        await eventually(lambda: sshd.signals == ["KILL"])
        assert await echo(ssh) == "ok\n"  # the connection and its session slot survive

    async def test_a_cancelled_command_is_killed(self, ssh: SSHTransport, sshd: SSHD):
        running = asyncio.create_task(ssh.run(["sleep", "30"], timeout=TIMEOUT))
        await sshd.started.wait()
        await asyncio.sleep(0.1)  # the client hears a moment later that it started
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await eventually(lambda: sshd.signals == ["KILL"])


class TestLogin:
    async def test_with_a_password(self, sshd: SSHD):
        async with transport(sshd.config(auth="password"), sshd.password()) as ssh:
            assert await echo(ssh) == "ok\n"

    async def test_with_a_key_and_its_passphrase(self, sshd: SSHD):
        key = sshd.key(passphrase=PASSPHRASE)
        async with transport(sshd.config(), key) as ssh:
            assert await echo(ssh) == "ok\n"

    async def test_a_wrong_password_is_rejected(self, sshd: SSHD):
        wrong = SSHPassword(value=WRONG)
        async with transport(sshd.config(auth="password"), wrong) as ssh:
            with pytest.raises(AuthenticationError, match="rejected the credentials"):
                await echo(ssh)

    @pytest.mark.parametrize("wrong", ["pem", "passphrase"])
    async def test_a_key_that_cannot_be_read_is_refused_without_quoting_it(
        self, sshd: SSHD, wrong: str
    ):
        key = sshd.key(passphrase=PASSPHRASE)
        if wrong == "pem":
            key = SSHPrivateKey(pem=b"-----BEGIN NONSENSE-----")
        else:
            key = SSHPrivateKey(pem=key.pem, passphrase=WRONG)
        async with transport(sshd.config(), key) as ssh:
            with pytest.raises(AuthenticationError, match="cannot be read") as caught:
                await echo(ssh)
        assert key.pem.get_secret_value().decode() not in str(caught.value)

    async def test_a_host_key_that_does_not_match_is_refused(self, sshd: SSHD):
        impostor = asyncssh.generate_private_key("ssh-ed25519")
        config = sshd.config(known_hosts=sshd.known_hosts(impostor))
        async with transport(config, sshd.key()) as ssh:
            with pytest.raises(AuthenticationError, match="host key"):
                await echo(ssh)

    async def test_nothing_in_dot_ssh_is_used(self, sshd: SSHD, home: Path):
        # Everything that would let it in, where a plain ssh client would look.
        dot_ssh = home / ".ssh"
        dot_ssh.mkdir(mode=0o700)
        (dot_ssh / "id_ed25519").write_bytes(sshd.client_key.export_private_key())
        (dot_ssh / "id_ed25519").chmod(0o600)
        (dot_ssh / "known_hosts").write_text(sshd.known_hosts() + "\n")
        impostor = asyncssh.generate_private_key("ssh-ed25519")
        unauthorized = SSHPrivateKey(pem=impostor.export_private_key())

        async with transport(sshd.config(), unauthorized) as ssh:
            with pytest.raises(AuthenticationError, match="rejected"):
                await echo(ssh)
        config = sshd.config(known_hosts=sshd.known_hosts(impostor))
        async with transport(config, sshd.key()) as ssh:
            with pytest.raises(AuthenticationError, match="host key"):
                await echo(ssh)


class FakeAgent:
    """Stands in for ssh-agent: hands out key pairs it signs with."""

    def __init__(self, keys: Sequence[asyncssh.SSHKeyPair]) -> None:
        self.keys = keys
        self.closed = False

    async def get_keys(self) -> Sequence[asyncssh.SSHKeyPair]:
        return self.keys

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class TestAgent:
    @pytest.fixture
    def agent(self, sshd: SSHD, monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
        agent = FakeAgent(asyncssh.load_keypairs([sshd.client_key]))

        async def connect_agent(*args: Any) -> FakeAgent:
            return agent

        monkeypatch.setattr(asyncssh, "connect_agent", connect_agent)
        return agent

    async def test_logs_in_with_the_agent_and_never_asks_the_provider(
        self, sshd: SSHD, agent: FakeAgent
    ):
        async with transport(sshd.config(auth="agent")) as ssh:  # no credentials
            assert await echo(ssh) == "ok\n"
        assert agent.closed  # only held open while connecting

    async def test_an_agent_without_keys_is_refused(self, sshd: SSHD, agent: FakeAgent):
        agent.keys = []
        async with transport(sshd.config(auth="agent")) as ssh:
            with pytest.raises(AuthenticationError, match="holds no keys"):
                await echo(ssh)

    async def test_no_agent_is_refused(
        self, sshd: SSHD, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        async with transport(sshd.config(auth="agent")) as ssh:
            with pytest.raises(AuthenticationError, match="no SSH agent"):
                await echo(ssh)
