"""The tracing wrappers pass exactly the protocol checks the objects they wrap pass."""

from __future__ import annotations

import pytest

from demo.events import Bus
from demo.tracing import (
    CAPABILITIES,
    Tracer,
    TracingProvider,
    TracingRunner,
    TracingScheduler,
    TracingTransport,
    capabilities,
    traced_backend,
)
from intercede import JobBackend, registry
from intercede._backends.batch.protocols import Scheduler, Transport
from intercede._backends.fake import FakeBackend
from intercede._backends.monolith.htcondorce.process import run_process
from intercede.auth.protocols import CredentialProvider
from intercede.auth.providers import StaticCredentials
from intercede.config import validate_config

SSH = {
    "kind": "batch",
    "transport": "ssh",
    "scheduler": "slurm",
    "host": "login.example.org",
    "user": "dirac",
    "known_hosts": "login.example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA",
}


@pytest.fixture
def tracer() -> Tracer:
    return Tracer(Bus())


def test_a_traced_fake_has_every_capability(tracer: Tracer):
    traced = traced_backend(FakeBackend(), "fake", "FAKE", tracer)
    assert capabilities(traced) == [protocol.__name__ for _, protocol in CAPABILITIES]
    assert isinstance(traced, JobBackend)


def test_a_traced_backend_claims_no_capability_it_lacks(tracer: Tracer):
    settings = validate_config(SSH)
    provider = TracingProvider(StaticCredentials(), tracer)
    transport = TracingTransport(
        registry._load("transport", "ssh").from_settings(settings, provider), tracer
    )
    scheduler = TracingScheduler(
        registry._load("scheduler", "slurm").from_settings(settings), tracer
    )
    inner = registry._load("backend", "batch").from_settings(
        settings, transport=transport, scheduler=scheduler
    )
    traced = traced_backend(inner, "ssh-slurm", "LIVE", tracer)
    assert capabilities(traced) == capabilities(inner)
    # BatchBackend has no live diagnostics, so neither may its wrapper.
    assert "Diagnosable" not in capabilities(traced)


def test_the_parts_pass_the_registrys_own_checks(tracer: Tracer):
    settings = validate_config(SSH)
    provider = TracingProvider(StaticCredentials(), tracer)
    assert isinstance(provider, CredentialProvider)
    transport = TracingTransport(
        registry._load("transport", "ssh").from_settings(settings, provider), tracer
    )
    scheduler = TracingScheduler(
        registry._load("scheduler", "slurm").from_settings(settings), tracer
    )
    assert registry._checked("transport", "ssh", transport, Transport) is transport
    assert registry._checked("scheduler", "slurm", scheduler, Scheduler) is scheduler


def test_the_htcondor_backend_takes_the_traced_runner(tracer: Tracer):
    settings = validate_config(
        {"kind": "htcondor-ce", "endpoint": "ce.example.org:9619"}
    )
    backend = registry._load("backend", "htcondor-ce")(
        settings, StaticCredentials(), runner=TracingRunner(run_process, tracer)
    )
    assert isinstance(traced_backend(backend, "htcondor", "LIVE", tracer), JobBackend)


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
