"""The tracing wrappers pass exactly the protocol checks the objects they wrap pass."""

from __future__ import annotations

import pytest

from demo import dryrun
from demo.build import build
from demo.events import Bus
from demo.tracing import (
    CAPABILITIES,
    Tracer,
    TracingProvider,
    capabilities,
    traced_backend,
)
from intercede import JobBackend
from intercede._backends.fake import FakeBackend
from intercede.auth.protocols import CredentialProvider
from intercede.auth.providers import StaticCredentials

# Every backend the stacks configure, as the dry run builds it.
TARGETS = dryrun.targets()


@pytest.fixture
def tracer() -> Tracer:
    return Tracer(Bus())


def test_a_traced_fake_has_every_capability(tracer: Tracer):
    traced = traced_backend(FakeBackend(), "fake", "FAKE", tracer)
    assert capabilities(traced) == [protocol.__name__ for _, protocol in CAPABILITIES]
    assert isinstance(traced, JobBackend)


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_a_traced_backend_claims_no_capability_it_lacks(tracer: Tracer, name: str):
    provider = TracingProvider(StaticCredentials(), tracer)
    assert isinstance(provider, CredentialProvider)
    traced = build(TARGETS[name][1], provider, tracer, name, "LIVE")
    assert isinstance(traced, JobBackend)
    assert capabilities(traced) == capabilities(traced.inner)


async def test_each_call_is_one_operation(tracer: Tracer):
    traced = traced_backend(FakeBackend(), "fake", "FAKE", tracer)
    async with traced:
        submission = await traced.submit(_spec(), 2)
        await traced.get_status(list(submission.handles.values()))
    names = [op.name for op in tracer.operations]
    assert names == ["submit", "get_status"]
    assert tracer.operations[0].outcome == {
        "handles": {"0": submission.handles[0].key, "1": submission.handles[1].key},
        "failures": {},
    }


def _spec():
    from intercede import SubmissionSpec

    return SubmissionSpec(executable="/bin/true")
