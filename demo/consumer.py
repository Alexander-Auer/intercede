"""The consumer: the code a WMS would write against interCEde, run unchanged for every backend.

The job board shows this file's source above its columns. It knows nothing of
any backend: it gets one from configuration, submits, stores the handles it is
given, and polls them in batches. Whatever differs per backend happens below it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from intercede import (
    CopyVars,
    Diagnosable,
    JobBackend,
    JobHandle,
    Killable,
    OutputRetriever,
    Purgeable,
    StatusMap,
    Submission,
    SubmissionSpec,
)


async def submit(
    backend: JobBackend,
    spec: SubmissionSpec,
    copies: Sequence[CopyVars],
    database: dict[str, str],
) -> Submission:
    """Submit every copy, and keep each handle before doing anything else with it."""
    submission = await backend.submit(spec, copies)
    for handle in submission.handles.values():
        database[handle.key] = handle.model_dump_json()  # the consumer's database
    return submission


async def poll(backend: JobBackend, handles: Sequence[JobHandle]) -> StatusMap:
    """Ask about many jobs in one call; a job the backend forgot comes back UNKNOWN."""
    return await backend.get_status(handles)


async def act(
    backend: JobBackend, operation: str, handles: Sequence[JobHandle], dest: Path
) -> dict:
    """Run an optional capability, after checking the backend has it."""
    if operation == "kill" and isinstance(backend, Killable):
        return dict(await backend.kill(handles))
    if operation == "purge" and isinstance(backend, Purgeable):
        return dict(await backend.purge(handles))
    if operation == "get_output" and isinstance(backend, OutputRetriever):
        return dict(await backend.get_output(handles, dest))
    if operation == "get_diagnostics" and isinstance(backend, Diagnosable):
        return dict(await backend.get_diagnostics(handles))
    raise NotImplementedError(f"this backend is not capable of {operation}")
