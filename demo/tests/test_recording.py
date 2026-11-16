"""Recording and replay, and what a recording may never hold."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
from pathlib import Path

import pytest

from demo import dryrun, recording
from demo.events import REDACTED, Bus
from demo.recording import Player, Recorder
from demo.tracing import Tracer, TracingProvider, _current, traced_backend
from intercede import CopyVars, SubmissionSpec, registry
from intercede._backends.fake import FakeBackend
from intercede.auth.models import BearerToken, CredentialRequirements, SSHPrivateKey
from intercede.auth.providers import StaticCredentials

TOKEN = "eyJhbGciOiJFUzI1NiJ9.a-token-that-must-never-show.c2lnbmF0dXJl"  # noqa: S105  (a fake)
KEY = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    b"b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    b"-----END OPENSSH PRIVATE KEY-----\n"
)
COPY_SECRET = b"pilot-secret-for-copy-zero-0123456789"


@pytest.fixture(autouse=True)
def recordings_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(recording, "RECORDINGS", tmp_path)
    return tmp_path


async def test_a_recording_holds_no_secret(recordings_here: Path):
    bus = Bus()
    tracer = Tracer(bus)
    recorder = Recorder(bus)
    recorder.start({"type": "hello"})

    # A provider hands out a token and a key: what any backend would ask for.
    provider = TracingProvider(
        StaticCredentials([BearerToken(value=TOKEN), SSHPrivateKey(pem=KEY)]), tracer
    )
    fake = traced_backend(FakeBackend(), "fake", "FAKE", tracer)
    async with provider, fake:
        tracer.operations.clear()
        operation = tracer.start("fake", "FAKE", "submit", {})
        token = _current.set(operation)  # as a backend's call would set it
        try:
            await provider.get(CredentialRequirements(resource="ce", kind=BearerToken))
            await provider.get(
                CredentialRequirements(resource="login", kind=SSHPrivateKey)
            )
        finally:
            _current.reset(token)
        # A copy's secret, submitted for real to the Fake...
        copies = [
            CopyVars(environment={"I": "0"}, secrets={"pilot.secret": COPY_SECRET})
        ]
        await fake.submit(SubmissionSpec(executable="/bin/true"), copies)
        # ...and printed by a tool, as a careless one might.
        bus.publish(
            {
                "type": "alert",
                "column": "fake",
                "message": f"tool said {TOKEN} and {COPY_SECRET.decode()}",
            }
        )

    # The dry run stages the secret into files and hands a token to its backend.
    form = {"executable": "python3", "copies": 1, "secret": "pilot.secret"}
    bus.publish({"type": "dryrun", **await dryrun.translate(form, launcher=False)})

    path = recorder.stop()
    # Decoded and dumped again without escapes, so every string is searched as it is.
    text = json.dumps(
        json.loads(gzip.decompress(path.read_bytes())), ensure_ascii=False
    )
    for secret in (TOKEN, KEY.decode(), COPY_SECRET.decode()):
        assert secret not in text
        assert base64.b64encode(secret.encode()).decode() not in text
    for line in KEY.decode().splitlines()[1:-1]:
        assert line not in text
    assert REDACTED in text  # the alert was scrubbed, not dropped


async def test_a_recording_plays_back_the_same_events(recordings_here: Path):
    bus = Bus()
    recorder = Recorder(bus)
    recorder.start({"type": "hello", "jobs": []})
    bus.publish({"type": "board", "jobs": [{"key": "a"}]})
    bus.publish(
        {"type": "op.start", "op": {"id": 7, "name": "get_status", "column": "x"}}
    )  # a poll: left out
    bus.publish({"type": "op.end", "id": 7, "outcome": {}})
    path = recorder.stop()

    data = recording.load(path)
    assert [e["event"]["type"] for e in data["events"]] == ["hello", "board"]
    assert recording.recordings()[0]["name"] == path.name

    queue = bus.listen()
    player = Player(bus)
    player.speed = 16
    player.play(path.name)
    bus.publish(
        {"type": "board", "jobs": [{"key": "live"}]}
    )  # live, while the recording plays
    await asyncio.wait_for(player._task, 5)
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    played = [e for e in seen if e.get("replayed") and e["type"] != "replay"]
    assert [e["type"] for e in played] == ["hello", "board"]
    assert all(e["jobs"] != [{"key": "live"}] for e in seen if e["type"] == "board")


def test_a_file_that_is_not_a_recording_is_refused(recordings_here: Path):
    path = recordings_here / "other.json.gz"
    path.write_bytes(gzip.compress(json.dumps({"format": 99}).encode()))
    with pytest.raises(ValueError, match="not a recording"):
        recording.load(path)


def test_the_dry_run_builds_backends_from_the_registry():
    assert set(dryrun.TARGETS) == {"htcondor-ce", "slurm"}
    for config in dryrun.TARGETS.values():
        assert registry.available()["backend"]  # the classes it loads are registered
        assert config["kind"] in registry.available()["backend"]
