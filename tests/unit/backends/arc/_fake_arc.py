"""A simulated A-REX, and helpers for driving the ARC backend with it.

``FakeARC`` answers the way ARC 7.2 does (checked against the arc-slurm stack):
one entry per job with its own status code, 404 for a job it does not know,
and HTTP 500 "User can't be assigned configuration" for a token it refuses.
A kill of a job that has ended is accepted and does nothing; a clean of a job
that is still active waits until it has ended. It serves a job's session
directory as A-REX does: listings without links or sizes, files with HEAD and
ranges from an offset, and the description it was sent.
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
ENDED = frozenset({"FINISHED", "FAILED", "KILLED", "WIPED"})


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
    files: dict[str, bytes] = field(default_factory=dict)  # its session directory
    log: bytes | None = None  # the CE's "errors" file of the job
    clean_when_ended: bool = False

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
    """Plays A-REX's REST interface: create, status, kill, clean, and uploads."""

    def __init__(self) -> None:
        super().__init__(self._handle)
        self.requests: list[Request] = []
        self.jobs: dict[str, FakeJob] = {}
        self.refuse_token = False  # answer every request as a refused token
        self.refused_copies: set[int] = set()  # descriptions the CE refuses
        self.failing_uploads: set[str] = set()  # names whose upload fails
        self.refused_kills: set[str] = set()  # jobs the CE will not kill
        self.ignore_ranges = False  # send all of a file whatever range is asked
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
        if request.method in ("GET", "HEAD") and path.startswith("jobs/"):
            return self._read(request, path.removeprefix("jobs/"))
        if request.method == "PUT" and path.startswith("jobs/"):
            job_id, _, name = path.removeprefix("jobs/").partition("/session/")
            if job_id not in self.jobs:
                return _response(404, "Job not found")
            if name in self.failing_uploads:
                return _response(500, "Failed to write file")
            self.jobs[job_id].files[name] = request.content
            return _response(200, "OK")
        return _response(501, "Not Implemented")

    def _read(self, request: httpx.Request, path: str) -> httpx.Response:
        job_id, _, rest = path.partition("/")
        job = self.jobs.get(job_id)
        kind, _, name = rest.partition("/")
        if job is None:
            return _response(404, "Job not found")
        if kind == "diagnose" and name == "description":
            return httpx.Response(200, content=ElementTree.tostring(job.description))
        if kind == "diagnose" and name == "errors" and job.log is not None:
            return httpx.Response(200, content=job.log)
        if kind != "session" or job.state == "WIPED":  # wiped: its files are gone
            return _response(404, "Not found")
        if name in job.files:
            content = job.files[name]
            size = {"Content-Length": str(len(content))}
            if request.method == "HEAD":
                return httpx.Response(200, headers=size)
            start = request.headers.get("Range", "").removeprefix("bytes=")
            if start.endswith("-") and not self.ignore_ranges:
                return httpx.Response(206, content=content[int(start[:-1]) :])
            return httpx.Response(200, content=content)
        return self._listing(job, name)

    def _listing(self, job: FakeJob, name: str) -> httpx.Response:
        prefix = f"{name.rstrip('/')}/" if name else ""
        files, dirs = set(), set()
        for path in job.files:
            if path.startswith(prefix):
                head, sep, _ = path.removeprefix(prefix).partition("/")
                (dirs if sep else files).add(head)
        if prefix and not files | dirs:
            return _response(404, "Not found")
        return httpx.Response(200, json={"file": sorted(files), "dir": sorted(dirs)})

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
            elif action == "kill" and job_id in self.refused_kills:
                entries.append(
                    {"status-code": "500", "reason": "Internal error", "id": job_id}
                )
            elif action == "kill":
                if job.state not in ENDED:
                    job.state = "KILLING"
                    if job.clean_when_ended:  # it is cleaned once it has stopped
                        del self.jobs[job_id]
                entries.append(
                    {"status-code": "202", "reason": "Queued for killing", "id": job_id}
                )
            elif action == "clean":
                if job.state in ENDED:
                    del self.jobs[job_id]
                else:
                    job.clean_when_ended = True
                entries.append(
                    {
                        "status-code": "202",
                        "reason": "Queued for cleaning",
                        "id": job_id,
                    }
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
