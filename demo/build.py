"""Which backends the demo shows, and how each is built: from the code and its configuration only.

Nothing here names a backend. What the demo shows comes from:

- ``tests/integration/stacks.yml``, and each stack's own ``intercede-client.toml``,
  read the way the integration suite reads them: which backends there are and
  how to reach them. A stack whose backend this version does not provide yet is
  left out, as the shared suite skips it.
- the registry: which class a configuration becomes.
- the configuration models: what kind of backend that is. A model with a
  transport and a scheduler is composed; one with a queue is a gateway that
  schedules by itself; one with neither boots its own resources.

A composed backend is put together as the registry puts it together, with its
transport and scheduler wrapped so each call is seen at their protocols. Every
other backend is built by ``registry.get_backend``, exactly as a consumer gets
it; what it sends to its resource is seen where it leaves the process
(``demo/doors.py``).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from tests.integration.conftest import _READERS
from tests.integration.generate_matrix import STACKS_DIR, compose_env, load_manifest

from demo.tracing import Tracer, TracingScheduler, TracingTransport, traced_backend
from intercede import config as config_models
from intercede import registry
from intercede._backends.batch.protocols import Scheduler, Transport
from intercede.config import validate_config


def stacks() -> dict[str, dict[str, Any]]:
    """The entries of ``tests/integration/stacks.yml``, by id."""
    return {entry["id"]: entry for entry in load_manifest()}


def compose_file(stack_id: str) -> Path:
    return STACKS_DIR / compose_env(stacks()[stack_id]) / "compose.yml"


def config_path(stack_id: str) -> Path:
    return STACKS_DIR / stack_id / "config" / "basic" / "intercede-client.toml"


def static_config(stack_id: str) -> dict[str, Any] | None:
    """A stack's ``basic`` client configuration as committed, or None when it has none."""
    path = config_path(stack_id)
    if not path.is_file():
        return None
    with path.open("rb") as f:
        return tomllib.load(f)


def client_config(stack_id: str) -> dict[str, Any]:
    """Read a stack's client configuration, as the integration suite does.

    The host key an SSH stack creates when it starts is added to its [backend].
    """
    config = static_config(stack_id)
    if config is None:
        raise FileNotFoundError(
            2, "no client configuration", str(config_path(stack_id))
        )
    if config.get("credentials", {}).get("type") == "ssh-key":
        known_hosts = (
            compose_file(stack_id).parent
            / config["credentials"]["path"]
            / "known_hosts"
        )
        config["backend"]["known_hosts"] = known_hosts.read_text()
    return config


def credential(stack_id: str, config: dict[str, Any]) -> Any:
    """Read the credential a stack minted at its start, as the integration suite does."""
    section = config["credentials"]
    return _READERS[section["type"]](compose_file(stack_id).parent / section["path"])


def trust(stack_id: str, config: dict[str, Any]) -> None:
    """Trust the CA a TLS stack made at start, as a grid host trusts its CAs.

    A stack that serves TLS writes its CA, in the ``X509_CERT_DIR`` layout, to
    ``certificates/`` in its credentials folder (the integration suite reads it
    from there too). OpenSSL reads ``X509_CERT_DIR`` as a list of folders, so
    each stack's is added to what is already there.
    """
    if "credentials" not in config:
        return
    folder = (
        compose_file(stack_id).parent / config["credentials"]["path"] / "certificates"
    )
    if not folder.is_dir():
        return
    present = [p for p in os.environ.get("X509_CERT_DIR", "").split(os.pathsep) if p]
    if str(folder) not in present:
        os.environ["X509_CERT_DIR"] = os.pathsep.join([*present, str(folder)])


def provided(stack_id: str) -> bool:
    """Tell whether this version provides the backend a stack's configuration asks for."""
    config = static_config(stack_id)
    return (
        config is not None
        and config["backend"]["kind"] in registry.available()["backend"]
    )


# -- what kind of backend a configuration describes ---------------------------


def model_of(backend: dict[str, Any]) -> type:
    """The configuration model a [backend] table is validated against.

    Chosen as ``validate_config`` chooses it, by its kind (and transport), so a
    table that needs what a stack makes at start still has a model.
    """
    for model in config_models._MODELS.values():
        fields = model.model_fields  # type: ignore[attr-defined]
        if backend.get("kind") not in registry_choices(model, "kind"):
            continue
        if "transport" in fields and backend.get("transport") not in registry_choices(
            model, "transport"
        ):
            continue
        return model
    return type(validate_config(backend))  # raises, saying what is wrong


def composed(model: type) -> bool:
    """A composed backend is configured with a transport and a scheduler."""
    fields = model.model_fields  # type: ignore[attr-defined]
    return "transport" in fields and "scheduler" in fields


def family(model: type) -> str:
    """Composed, a gateway that schedules by itself (it has a queue), or a provider."""
    if composed(model):
        return "composed"
    return "monolithic" if "queue" in model.model_fields else "provider"  # type: ignore[attr-defined]


def title(backend: dict[str, Any]) -> str:
    """What the board calls a backend: its parts when composed, else its class."""
    if composed(model_of(backend)):
        return f"{backend['transport']} + {backend['scheduler']}"
    return registry._load("backend", backend["kind"]).__name__.removesuffix("Backend")


def registry_choices(model: type, field: str) -> list[str]:
    """The values a configuration model's ``Literal`` field allows."""
    return sorted(registry._choices(model.model_fields[field].annotation))  # type: ignore[attr-defined]


def composed_models() -> list[type]:
    """Every configuration model of the composed backend, one per kind of transport."""
    return [m for m in config_models._MODELS.values() if composed(m)]


# -- building ----------------------------------------------------------------------


def build(
    backend: dict[str, Any],
    provider: Any,
    tracer: Tracer,
    column: str,
    source: str,
    *,
    transport: Any = None,
) -> Any:
    """Build the backend ``backend`` describes, traced; not yet opened.

    Args:
        backend: The [backend] table of a client configuration.
        provider: The consumer's credential provider.
        tracer: Where the calls are published.
        column: The board's name for it.
        source: LIVE, FAKE or DRY RUN, for the badges.
        transport: For a composed backend, a transport to use instead of the
            one the configuration names (the dry run's recorder).
    """
    settings: Any = validate_config(backend)
    if not composed(type(settings)):
        inner = registry.get_backend(settings, provider)
        return traced_backend(inner, column, source, tracer)
    raw = transport or registry._load("transport", settings.transport).from_settings(
        settings, provider
    )
    traced_transport = TracingTransport(raw, tracer)
    scheduler = TracingScheduler(
        registry._load("scheduler", settings.scheduler).from_settings(settings), tracer
    )
    # The same checks the registry makes before composing them.
    registry._checked("transport", settings.transport, traced_transport, Transport)
    registry._checked("scheduler", settings.scheduler, scheduler, Scheduler)
    inner = registry._load("backend", settings.kind).from_settings(
        settings, transport=traced_transport, scheduler=scheduler
    )
    return traced_backend(inner, column, source, tracer)
