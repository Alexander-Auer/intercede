"""The static views, read from the library's code, so the slides cannot drift from it.

- The contract: the protocols in ``intercede.protocols``, essential when
  ``JobBackend`` is built from them, optional otherwise, with their docstrings.
- The backends: every name a configuration can ask for (the ``Literal`` choices
  of the configuration models), implemented when the registry's tables list it,
  planned otherwise.
- Verified: a combination one of the stacks in ``tests/integration/stacks.yml``
  is configured for, so CI runs the shared suite against it.
"""

from __future__ import annotations

import inspect
import tomllib
from typing import Any, Protocol, get_args

from tests.integration.generate_matrix import STACKS_DIR

from demo import build
from demo.build import stacks
from intercede import config, protocols, registry
from intercede.auth import protocols as auth_protocols


def _doc(obj: Any) -> str:
    return inspect.getdoc(obj) or ""


def _choices(model: type, field: str) -> list[str]:
    annotation = model.model_fields[field].annotation  # type: ignore[attr-defined]
    return [a for a in get_args(annotation) if isinstance(a, str)]


def _class_of(group: str, name: str) -> Any:
    try:
        return registry._load(group, name)
    except Exception:  # a planned entry: nothing to load
        return None


def architecture() -> dict[str, Any]:
    essential = [
        base for base in protocols.JobBackend.__bases__ if base is not Protocol
    ]
    contract = []
    for name in protocols.__all__:
        protocol = getattr(protocols, name)
        if protocol is protocols.JobBackend:
            continue
        methods = [
            {
                "name": m,
                "signature": f"{m}{inspect.signature(getattr(protocol, m))}",
                "doc": _doc(getattr(protocol, m)),
            }
            for m in protocol.__dict__
            if not m.startswith("_") and callable(getattr(protocol, m))
        ]
        contract.append(
            {
                "protocol": name,
                "essential": protocol in essential,
                "doc": _doc(protocol),
                "methods": methods,
            }
        )
    contract.sort(
        key=lambda p: (not p["essential"], _ORDER.index(p["methods"][0]["name"]))
    )
    available = registry.available()
    kinds = sorted(
        {kind for model in config._MODELS.values() for kind in _choices(model, "kind")}
    )
    backends = []
    for kind in kinds:
        cls = _class_of("backend", kind) if kind in available["backend"] else None
        model = next(
            m for key, m in config._MODELS.items() if key.split("+")[0] == kind
        )
        backends.append(
            {
                "kind": kind,
                "family": build.family(model),
                "implemented": cls is not None,
                "class": cls.__name__ if cls else None,
                "doc": _doc(cls) if cls else _doc(model),
                "config": model.__name__,
            }
        )
    return {
        "contract": contract,
        "backends": backends,
        "registry": {"doc": _doc(registry), "get_backend": _doc(registry.get_backend)},
        "provider": {"doc": _doc(auth_protocols.CredentialProvider)},
        "job_backend": {"doc": _doc(protocols.JobBackend)},
    }


# The contract's operations in the order a job meets them.
_ORDER = ["submit", "get_status", "get_output", "kill", "purge", "get_diagnostics"]


def composition() -> dict[str, Any]:
    available = registry.available()
    models = build.composed_models()
    transports = sorted({t for model in models for t in _choices(model, "transport")})
    schedulers = sorted({s for model in models for s in _choices(model, "scheduler")})
    verified = _verified()
    rows = []
    for transport in transports:
        model = next(m for m in models if transport in _choices(m, "transport"))
        variants: list[tuple[str, str | None]] = [(transport, None)]
        if "jump_host" in model.model_fields:  # type: ignore[attr-defined]
            variants.append((transport, "jump_host"))
        for name, variant in variants:
            cls = (
                _class_of("transport", name) if name in available["transport"] else None
            )
            rows.append(
                {
                    "transport": name,
                    "variant": variant,
                    "label": f"{name} via a jump host" if variant else name,
                    "class": cls.__name__ if cls else None,
                    "doc": (
                        model.model_fields["jump_host"].description  # type: ignore[attr-defined]
                        or ""
                    )
                    if variant
                    else _doc(cls),
                    "cells": [
                        {
                            "scheduler": scheduler,
                            "state": _state(
                                name, scheduler, variant, available, verified
                            ),
                            "stacks": sorted(
                                verified.get((name, scheduler, variant), [])
                            ),
                        }
                        for scheduler in schedulers
                    ],
                }
            )
    columns = [
        {
            "scheduler": s,
            "class": (c := _class_of("scheduler", s)) and c.__name__,
            "doc": _doc(c) if c else "",
        }
        for s in schedulers
    ]
    monolithic = [
        {**b, "stacks": sorted(verified.get((b["kind"], None, None), []))}
        for b in architecture()["backends"]
        if b["family"] != "composed"
    ]
    for b in monolithic:
        b["state"] = (
            "verified"
            if b["stacks"]
            else "implemented"
            if b["implemented"]
            else "planned"
        )
    listed_without_folder = [s for s in stacks() if not (STACKS_DIR / s).is_dir()]
    composed_kinds = {k for m in models for k in _choices(m, "kind")}
    return {
        "composed_class": " / ".join(
            sorted(c.__name__ for k in composed_kinds if (c := _class_of("backend", k)))
        ),
        "transports": rows,
        "schedulers": columns,
        "others": monolithic,
        "listed_without_folder": listed_without_folder,
    }


def _state(
    transport: str,
    scheduler: str,
    variant: str | None,
    available: dict[str, Any],
    verified: dict[Any, Any],
) -> str:
    if (
        transport not in available["transport"]
        or scheduler not in available["scheduler"]
    ):
        return "planned"
    return "verified" if (transport, scheduler, variant) in verified else "implemented"


def _verified() -> dict[tuple[str, str | None, str | None], list[str]]:
    """What each stack's client configuration asks for: what CI runs the suite against."""
    found: dict[tuple[str, str | None, str | None], list[str]] = {}
    for stack_id in stacks():
        path = STACKS_DIR / stack_id / "config" / "basic" / "intercede-client.toml"
        if not path.is_file():
            continue
        backend = tomllib.loads(path.read_text())["backend"]
        if "transport" in backend and "scheduler" in backend:  # composed
            key = (
                backend["transport"],
                backend["scheduler"],
                "jump_host" if backend.get("jump_host") else None,
            )
        else:
            key = (backend["kind"], None, None)
        found.setdefault(key, []).append(stack_id)
    return found
