"""The class diagram is read from the package: what the code says, and nothing the demo adds."""

from __future__ import annotations

import inspect

import pytest

import intercede
from demo import classes
from intercede import models, registry
from intercede.models import SubmissionSpec


@pytest.fixture(scope="module")
def diagram() -> dict:
    return classes.model()


def _named(diagram: dict) -> dict[str, dict]:
    return {c["name"]: c for c in diagram["classes"]}


def _pairs(diagram: dict, kind: str) -> set[tuple[str, str]]:
    name = {c["id"]: c["name"] for c in diagram["classes"]}
    return {
        (name[r["from"]], name[r["to"]])
        for r in diagram["relations"]
        if r["kind"] == kind
    }


def test_every_public_class_is_drawn(diagram):
    public = {
        name
        for name in intercede.__all__
        if inspect.isclass(getattr(intercede, name))
        and getattr(intercede, name).__module__.startswith(intercede.__name__)
    }
    assert public <= set(_named(diagram))


def test_every_backend_the_registry_has_is_a_job_backend(diagram):
    implements = _pairs(diagram, "implements")
    for kind in registry.available()["backend"]:
        cls = registry._load("backend", kind)
        assert (cls.__name__, "JobBackend") in implements, kind


def test_every_protocol_the_code_declares_is_drawn(diagram):
    """``name: type[P] = C`` is checked by mypy; the diagram must show the same."""
    implements = _pairs(diagram, "implements")
    named = _named(diagram)
    by_name = {c["name"]: c for c in diagram["classes"]}
    inherits = _pairs(diagram, "inherits")

    def covered(cls: str, protocol: str) -> bool:
        if (cls, protocol) in implements:
            return True
        # drawn as a protocol made of this one, such as JobBackend for Submitter
        return any(
            (cls, wider) in implements and (wider, protocol) in inherits
            for wider in by_name
        )

    declared = classes.declared()
    assert declared
    assert [pair for pair in declared if not covered(*pair)] == []
    assert all(cls in named for cls, _ in declared)


def test_a_method_name_alone_does_not_implement_a_protocol(diagram):
    """Only a class whose methods take the protocol's parameters is drawn as implementing it."""
    for cls, protocol in _pairs(diagram, "implements"):
        assert _named(diagram)[protocol]["kind"] == "protocol"
        wanted = {
            m["name"]: m["parameters"] for m in _named(diagram)[protocol]["methods"]
        }
        have = {m["name"]: m["parameters"] for m in _named(diagram)[cls]["methods"]}
        for name, parameters in wanted.items():
            if name in have:
                assert have[name] == parameters, (cls, protocol, name)


def test_relations_join_drawn_classes(diagram):
    ids = {c["id"] for c in diagram["classes"]}
    assert diagram["relations"]
    for relation in diagram["relations"]:
        assert relation["from"] in ids and relation["to"] in ids
        assert relation["from"] != relation["to"]


def test_a_class_added_to_the_package_appears_without_a_demo_change(monkeypatch):
    added = type("AddedLater", (models.JobHandle,), {"__module__": models.__name__})
    monkeypatch.setattr(models, "AddedLater", added, raising=False)
    drawn = classes.model.__wrapped__()
    assert "AddedLater" in _named(drawn)
    assert ("AddedLater", "JobHandle") in _pairs(drawn, "inherits")


class _Built:
    """Builds what it holds, as a backend builds the parts of its capabilities."""

    def __init__(self) -> None:
        self.spec = SubmissionSpec(executable="/bin/true")
        self.name = str("x")  # a call, but not of a class in the module


def test_an_attribute_built_in_init_is_held(monkeypatch):
    monkeypatch.setattr(_Built, "__module__", models.__name__)
    monkeypatch.setattr(models, "_Built", _Built, raising=False)
    drawn = classes.model.__wrapped__()
    assert ("_Built", "SubmissionSpec") in _pairs(drawn, "holds")
    fields = {f["name"]: f["type"] for f in _named(drawn)["_Built"]["fields"]}
    assert fields["name"] == ""


def test_source_is_served_only_for_a_drawn_class(diagram):
    first = diagram["classes"][0]
    shown = classes.source(first["id"])
    assert shown is not None
    assert f"class {first['name']}" in shown["source"]
    assert classes.source("../../etc/passwd") is None
    assert classes.source("c999999") is None
