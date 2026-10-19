"""What each backend needs from the public ``BackendConfig`` (IC-ADR-001 §5).

The consumer describes a backend with one flat model. ``resolve`` turns it into
the settings of the one backend it names, and is where per-kind rules live: a
field that does not apply to the chosen kind is rejected, not ignored.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import Discriminator, PositiveInt, TypeAdapter, model_validator

from intercede.models import _Model

if TYPE_CHECKING:
    from intercede.config import BackendConfig

LIMIT_FIELDS = ("max_output_bytes", "max_output_files", "transfer_timeout_s")
SSH_FIELDS = ("host", "port", "user", "known_hosts", "jump_host", "connect_timeout_s")


class Limits(_Model):
    """Bounds on output collection (IC-ADR-001 §3). Path containment is always on."""

    max_output_bytes: PositiveInt = 10 * 2**30
    max_output_files: PositiveInt = 10_000
    transfer_timeout_s: PositiveInt = 3600


class _BackendSettings(_Model):
    queue: str | None = None
    container_launcher: Sequence[str] | None = None
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _launcher_names_the_image(self) -> Self:
        launcher = self.container_launcher
        if launcher is not None and not any("{image}" in arg for arg in launcher):
            raise ValueError("container_launcher must contain an '{image}' placeholder")
        return self


# -- monolithic backends ------------------------------------------------------


class ARCSettings(_BackendSettings):
    kind: Literal["arc"]
    endpoint: str
    # All of these together (IC-ADR-003 §2): a token for the channel, a proxy
    # for delegation, or both.
    auth: frozenset[Literal["token", "proxy"]] = frozenset({"token"})

    @model_validator(mode="after")
    def _auth_not_empty(self) -> Self:
        if not self.auth:
            raise ValueError("auth must name at least one of 'token', 'proxy'")
        return self


class HTCondorCESettings(_BackendSettings):
    kind: Literal["htcondor-ce"]
    endpoint: str
    # The token authenticates the channel; "proxy" adds an X.509 proxy to the
    # submit description for sites that account on VOMS attributes.
    auth: frozenset[Literal["token", "proxy"]] = frozenset({"token"})

    @model_validator(mode="after")
    def _token_required(self) -> Self:
        if "token" not in self.auth:
            raise ValueError(
                "an HTCondor-CE authenticates with a token; auth must include 'token'"
            )
        return self


# -- composed backends --------------------------------------------------------


class LocalTransportSettings(_Model):
    kind: Literal["local"]


class SSHTransportSettings(_Model):
    kind: Literal["ssh"]
    host: str
    port: PositiveInt = 22
    user: str
    known_hosts: str  # host key verification is not optional (IC-ADR-003 §6)
    auth: Literal["key", "password", "agent"] = "key"
    jump_host: str | None = None
    connect_timeout_s: PositiveInt = 30

    @model_validator(mode="after")
    def _known_hosts_not_empty(self) -> Self:
        if not self.known_hosts.strip():
            raise ValueError("known_hosts must hold the expected host keys")
        return self


class BatchSettings(_BackendSettings):
    kind: Literal["batch"]
    transport: Annotated[
        LocalTransportSettings | SSHTransportSettings, Discriminator("kind")
    ]
    scheduler: Literal["slurm", "htcondor", "direct"]


BackendSettings = Annotated[
    ARCSettings | HTCondorCESettings | BatchSettings, Discriminator("kind")
]

_SETTINGS: TypeAdapter[BackendSettings] = TypeAdapter(BackendSettings)


def resolve(config: BackendConfig) -> BackendSettings:
    """Turn the consumer's flat configuration into the settings of one backend.

    Raises:
        pydantic.ValidationError: A field is missing, invalid, or does not
            apply to the chosen kind.

    """
    data: dict[str, Any] = config.model_dump(exclude_none=True)
    limits = {name: data.pop(name) for name in LIMIT_FIELDS if name in data}
    if limits:
        data["limits"] = limits

    if data["kind"] == "batch":
        # The transport's own fields, and the transport kind, move into one
        # nested object; anything left over is checked against the backend.
        transport = {name: data.pop(name) for name in SSH_FIELDS if name in data}
        if "transport" in data:
            transport["kind"] = data.pop("transport")
        if "auth" in data:
            auth = data.pop("auth")
            # An SSH connection authenticates one way, chosen here.
            transport["auth"] = next(iter(auth)) if len(auth) == 1 else sorted(auth)
        data["transport"] = transport

    return _SETTINGS.validate_python(data)
