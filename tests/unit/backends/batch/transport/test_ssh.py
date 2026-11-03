"""What only the SSH transport does: one connection, logging in, host keys, the jump host.

What every transport does is in ``test_conformance.py``, which runs over SSH too.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from intercede._backends.batch.transport.ssh import SSHTransport, route
from intercede.auth import (
    Credential,
    SSHPassword,
    SSHPrivateKey,
    StaticCredentials,
)
from intercede.config import BatchSSHConfig
from intercede.exceptions import AuthenticationError, ResourceUnavailableError

from ._sshd import HOST, SSHD, USER

TIMEOUT = 30  # seconds
CLOSED_PORT = 1  # nothing listens there, so connecting is refused at once
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
    async def test_building_and_entering_neither_connect_nor_ask_for_credentials(
        self, sshd: SSHD
    ):
        # No credentials at all: asking the provider would fail.
        async with transport(sshd.config(port=CLOSED_PORT)):
            pass

    async def test_an_unreachable_host_is_unavailable(self, sshd: SSHD):
        async with transport(sshd.config(port=CLOSED_PORT), sshd.key()) as ssh:
            with pytest.raises(ResourceUnavailableError, match="failed"):
                await echo(ssh)

    async def test_one_connection_serves_every_call(
        self, ssh: SSHTransport, sshd: SSHD, here: Path
    ):
        (here / "file").write_text("x")
        await asyncio.gather(*(echo(ssh, str(n)) for n in range(20)))
        await ssh.put(here / "file", "file", timeout=TIMEOUT)
        await ssh.get("file", here / "back", timeout=TIMEOUT)
        assert sshd.logins == 1

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

    async def test_closing_disconnects(self, sshd: SSHD):
        async with transport(sshd.config(), sshd.key()) as ssh:
            await echo(ssh)
            assert sshd.connected == 1
        await eventually(lambda: sshd.connected == 0)


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

    async def test_a_key_that_is_not_authorized_is_rejected(self, sshd: SSHD):
        other = asyncssh.generate_private_key("ssh-ed25519")
        key = SSHPrivateKey(pem=other.export_private_key())
        async with transport(sshd.config(), key) as ssh:
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


class TestJumpHost:
    @pytest.fixture
    async def gateway(self, sshd: SSHD, home: Path) -> AsyncIterator[SSHD]:
        async with SSHD(home, client_key=sshd.client_key) as server:
            yield server

    @pytest.mark.parametrize("named_user", [True, False], ids=["user@host", "host"])
    async def test_connects_through_it(
        self, sshd: SSHD, gateway: SSHD, named_user: bool
    ):
        jump_host = (
            f"{USER}@{HOST}:{gateway.port}" if named_user else f"{HOST}:{gateway.port}"
        )
        config = sshd.config(
            jump_host=jump_host,
            known_hosts=f"{gateway.known_hosts()}\n{sshd.known_hosts()}",
        )
        async with transport(config, sshd.key()) as ssh:
            assert await echo(ssh) == "ok\n"
        assert (gateway.logins, gateway.forwarded, sshd.logins) == (1, 1, 1)
        await eventually(lambda: gateway.connected == 0 == sshd.connected)

    async def test_its_host_key_is_checked_too(self, sshd: SSHD, gateway: SSHD):
        config = sshd.config(jump_host=f"{HOST}:{gateway.port}")  # only sshd's key
        async with transport(config, sshd.key()) as ssh:
            with pytest.raises(AuthenticationError, match="host key"):
                await echo(ssh)
        assert sshd.logins == 0

    def test_the_route_defaults_the_user_and_port(self, sshd: SSHD):
        config = sshd.config(jump_host="gw.example.org")
        assert route(config) == [
            (USER, "gw.example.org", 22),
            (USER, HOST, sshd.port),
        ]
