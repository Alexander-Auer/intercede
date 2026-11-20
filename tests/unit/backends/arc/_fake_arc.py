"""A simulated A-REX, and helpers for driving the ARC backend with it.

``FakeARC`` answers the way ARC 7.2 does (checked against the arc-slurm stack):
one entry per job with its own status code, 404 for a job it does not know,
and HTTP 500 "User can't be assigned configuration" for a token it refuses.
It runs behind httpx's ``MockTransport``, so no network is involved.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

import httpx

from intercede import ARCConfig, SubmissionSpec
from intercede._backends._arc.description import ADL_NAMESPACE
from intercede._backends.monolith.arc import ARCBackend
from intercede.auth.models import BearerToken, Credential, CredentialRequirements

ENDPOINT = "https://arc.example.org:443/arex"
API = "/arex/rest/1.1/"
# A command on the worker: no file of that name on this host, so nothing is uploaded.
ON_THE_WORKER = "/opt/worker-only/bin/run"
SPEC = SubmissionSpec(executable=ON_THE_WORKER)
NO_CONFIGURATION = "User can't be assigned configuration"


class ScopedTokens:
    """A provider whose token names the scopes it was asked for, so a request shows them."""

    def __init__(self) -> None:
        self.asked: list[frozenset[str]] = []

    async def get(self, requirements: CredentialRequirements) -> Credential:
        self.asked.append(requirements.scopes)
        return BearerToken(value="token:" + ",".join(sorted(requirements.scopes)))


@dataclass
class Request:
    method: str
    path: str  # relative to the interface, e.g. "jobs" or "jobs/<id>/session/a"
    action: str | None
    scopes: frozenset[str]  # read back from the token
    body: bytes


@dataclass
class FakeJob:
    id: str
    description: ElementTree.Element
    state: str = "ACCEPTING"
    files: dict[str, bytes] = field(default_factory=dict)

    def texts(self, tag: str) -> list[str]:
        """Return the text of every element named ``tag`` in the description."""
        return [
            element.text or ""
            for element in self.description.iter(f"{{{ADL_NAMESPACE}}}{tag}")
        ]

    def environment(self) -> dict[str, str]:
        tag = f"{{{ADL_NAMESPACE}}}"
        return {
            variable.findtext(f"{tag}Name", ""): variable.findtext(f"{tag}Value", "")
            for variable in self.description.iter(f"{tag}Environment")
        }

    def inputs(self) -> dict[str, str | None]:
        """Return each input file's name, with its source URL when it has one."""
        tag = f"{{{ADL_NAMESPACE}}}"
        return {
            entry.findtext(f"{tag}Name", ""): entry.findtext(f"{tag}Source/{tag}URI")
            for entry in self.description.iter(f"{tag}InputFile")
        }


class FakeARC(httpx.MockTransport):
    """Plays A-REX's REST interface: create, status, kill, and uploads."""

    def __init__(self) -> None:
        super().__init__(self._handle)
        self.requests: list[Request] = []
        self.jobs: dict[str, FakeJob] = {}
        self.refuse_token = False  # answer every request as a refused token
        self.refused_copies: set[int] = set()  # descriptions the CE refuses
        self.failing_uploads: set[str] = set()  # names whose upload fails
        self.answer: httpx.Response | None = None  # forced answer to everything
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1

    def actions(self) -> list[str]:
        return [request.action or request.method for request in self.requests]

    def created(self) -> list[FakeJob]:
        return list(self.jobs.values())

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path).removeprefix(API)
        token = request.headers.get("Authorization", "").removeprefix("Bearer token:")
        action = request.url.params.get("action")
        self.requests.append(
            Request(
                request.method,
                path,
                action,
                frozenset(token.split(",")) - {""},
                request.content,
            )
        )
        if self.refuse_token:
            return _response(500, NO_CONFIGURATION)
        if self.answer is not None:
            return self.answer
        if request.method == "POST" and path == "jobs" and action == "new":
            return self._new(request.content)
        if request.method == "POST" and path == "jobs":
            ids = [job["id"] for job in json.loads(request.content)["job"]]
            return self._about(action or "", ids)
        if request.method == "PUT" and path.startswith("jobs/"):
            job_id, _, name = path.removeprefix("jobs/").partition("/session/")
            if job_id not in self.jobs:
                return _response(404, "Job not found")
            if name in self.failing_uploads:
                return _response(500, "Failed to write file")
            self.jobs[job_id].files[name] = request.content
            return _response(200, "OK")
        return _response(501, "Not Implemented")

    def _new(self, body: bytes) -> httpx.Response:
        root = ElementTree.fromstring(body)  # noqa: S314  (the backend's own XML)
        descriptions = (
            [root] if root.tag.endswith("ActivityDescription") else list(root)
        )
        entries: list[dict[str, Any]] = []
        for index, description in enumerate(descriptions):
            if index in self.refused_copies:
                entries.append(
                    {"status-code": "500", "reason": "Queue is not available"}
                )
                continue
            job = FakeJob(secrets.token_hex(6), description)
            self.jobs[job.id] = job
            entries.append(
                {
                    "status-code": "201",
                    "reason": "Created",
                    "id": job.id,
                    "state": job.state,
                }
            )
        return httpx.Response(201, json={"job": entries})

    def _about(self, action: str, ids: Sequence[str]) -> httpx.Response:
        entries: list[dict[str, Any]] = []
        for job_id in ids:
            job = self.jobs.get(job_id)
            if job is None:
                entries.append(
                    {
                        "status-code": "404",
                        "reason": "Job not found",
                        "id": job_id,
                        "state": "None",
                    }
                )
            elif action == "status":
                entries.append(
                    {
                        "status-code": "200",
                        "reason": "OK",
                        "id": job_id,
                        "state": job.state,
                    }
                )
            elif action == "kill":
                job.state = "KILLING"
                entries.append(
                    {"status-code": "202", "reason": "Queued for killing", "id": job_id}
                )
        return httpx.Response(201, json={"job": entries})


def _response(code: int, reason: str) -> httpx.Response:
    return httpx.Response(code, extensions={"reason_phrase": reason.encode()})


def settings(**overrides: Any) -> ARCConfig:
    return ARCConfig(kind="arc", endpoint=ENDPOINT, **overrides)


def make(
    arc: FakeARC, tokens: ScopedTokens | None = None, **overrides: Any
) -> ARCBackend:
    return ARCBackend(settings(**overrides), tokens or ScopedTokens(), transport=arc)
