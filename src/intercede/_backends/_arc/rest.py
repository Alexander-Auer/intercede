"""ARC's REST interface: where each request goes, and what comes back.

Paths and bodies are built here and answers read here; the requests themselves
are the backend's. Every path is relative to ``api_url(endpoint)``.

A request about many jobs gets one entry per job back, each with its own
status code, so a bulk request can partly succeed. Version 1.0 of the
interface wrote a single entry, or a single file name, as a value instead of
a list; both are read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

API_VERSION = "1.1"

# What ARC keeps about a job next to its session directory, readable through
# the "diagnose" path: "errors" is the CE's log of the job, "failed" says why
# it failed.
DIAGNOSE_FILES = frozenset(
    {
        "failed",
        "local",
        "errors",
        "description",
        "diag",
        "comment",
        "status",
        "acl",
        "xml",
        "input",
        "output",
        "input_status",
        "output_status",
        "statistics",
    }
)

# ARC's job IDs are random letters and digits; anything else never reaches a URL.
_JOB_ID = re.compile(r"[A-Za-z0-9]+")


def api_url(endpoint: str) -> str:
    """Return the base URL of every request to the CE at ``endpoint``.

    Args:
        endpoint: The CE's A-REX URL, e.g. ``"https://arc.example.org:443/arex"``.

    Returns:
        The URL of the interface version interCEde speaks, ending in ``/``.
    """
    return f"{endpoint.rstrip('/')}/rest/{API_VERSION}/"


def is_job_id(value: str) -> bool:
    """Tell whether ``value`` looks like an ARC job ID."""
    return _JOB_ID.fullmatch(value) is not None


def jobs_body(ids: Sequence[str]) -> dict[str, Any]:
    """Return the JSON body of a ``jobs?action=...`` request about ``ids``."""
    return {"job": [{"id": job_id} for job_id in ids]}


def session_path(job_id: str, name: str = "") -> str:
    """Return the path of a file or directory in a job's session directory.

    Args:
        job_id: The job, as ARC named it.
        name: Relative to the session directory; empty for the directory itself.

    Returns:
        The path, quoted for a URL.

    Raises:
        ValueError: The job ID is not one, or ``name`` leaves the directory.
    """
    _check_job_id(job_id)
    if name:
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts:
            raise ValueError(f"{name!r} is not inside the session directory")
    return f"jobs/{job_id}/session/{quote(name)}"


def diagnose_path(job_id: str, name: str) -> str:
    """Return the path of one of ``DIAGNOSE_FILES`` of a job.

    Raises:
        ValueError: The job ID is not one, or ``name`` is not a diagnose file.
    """
    _check_job_id(job_id)
    if name not in DIAGNOSE_FILES:
        raise ValueError(f"{name!r} is not a file ARC keeps about a job")
    return f"jobs/{job_id}/diagnose/{name}"


def _check_job_id(job_id: str) -> None:
    if not is_job_id(job_id):
        raise ValueError(f"{job_id!r} is not an ARC job ID")


@dataclass(frozen=True)
class JobAnswer:
    """What ARC answered about one job of a request."""

    code: int  # an HTTP status code, for this job alone
    reason: str
    id: str | None = None  # absent when a job could not be created
    state: str | None = None  # in answers to "new" and "status"

    @property
    def ok(self) -> bool:
        return 200 <= self.code < 300


@dataclass(frozen=True)
class Listing:
    """The contents of one directory of a session directory."""

    files: tuple[str, ...] = ()
    dirs: tuple[str, ...] = ()


def parse_jobs(raw: str) -> list[JobAnswer]:
    """Read the answer to a ``jobs?action=...`` request.

    Args:
        raw: The response body.

    Returns:
        One answer per job, in the order of the request.

    Raises:
        ValueError: The body is not such an answer.
    """
    if not raw.strip():
        return []
    entries = _as_list(_object(raw).get("job"))
    return [_job_answer(entry) for entry in entries]


def parse_listing(raw: str) -> Listing:
    """Read the answer to a request for a directory of a session directory.

    ARC answers an empty directory with an empty body.

    Raises:
        ValueError: The body is not a listing.
    """
    if not raw.strip():
        return Listing()
    listing = _object(raw)
    return Listing(
        files=_names(listing.get("file"), "file"),
        dirs=_names(listing.get("dirs"), "dirs"),
    )


def _object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _job_answer(entry: Any) -> JobAnswer:
    if not isinstance(entry, dict):
        raise ValueError("expected an object per job")
    try:
        code = int(entry["status-code"])  # a string in some versions
        reason = entry["reason"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("a job's answer has no valid status-code and reason") from exc
    job_id, state = entry.get("id"), entry.get("state")
    if not all(isinstance(value, str | None) for value in (reason, job_id, state)):
        raise ValueError("a job's id, state and reason must be strings")
    return JobAnswer(code=code, reason=reason or "", id=job_id, state=state)


def _names(value: Any, key: str) -> tuple[str, ...]:
    names = _as_list(value)
    if not all(isinstance(name, str) for name in names):
        raise ValueError(f"expected the names in {key!r} to be strings")
    return tuple(names)
