"""From what ``squeue`` and ``sacct`` print to ``JobStatus``.

``squeue`` knows a job until a while after it ended (``MinJobAge``, 5 minutes by
default). After that only the accounting does, if the site keeps one: without it
a finished job is simply no longer known, and so ``UNKNOWN``.

The state map follows DIRAC's: PENDING, SUSPENDED and CONFIGURING wait, COMPLETED
is done, CANCELLED and PREEMPTED are aborted. The others follow from those.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence

from intercede._backends._slurm.commands import failure
from intercede._backends.batch.protocols import CommandResult
from intercede.models import JobID, JobStatus

_STATES: Mapping[str, JobStatus] = {
    # waiting for the resource, or to get it back
    "PENDING": JobStatus.WAITING,
    "CONFIGURING": JobStatus.WAITING,
    "SUSPENDED": JobStatus.WAITING,
    "STOPPED": JobStatus.WAITING,
    "REQUEUED": JobStatus.WAITING,
    "REQUEUE_FED": JobStatus.WAITING,
    "REQUEUE_HOLD": JobStatus.WAITING,
    "RESV_DEL_HOLD": JobStatus.WAITING,
    "SPECIAL_EXIT": JobStatus.WAITING,
    # running, including the steps that end it
    "RUNNING": JobStatus.RUNNING,
    "RESIZING": JobStatus.RUNNING,
    "SIGNALING": JobStatus.RUNNING,
    "COMPLETING": JobStatus.RUNNING,
    "STAGE_OUT": JobStatus.RUNNING,
    "COMPLETED": JobStatus.DONE,
    # it ran, and that went wrong
    "FAILED": JobStatus.FAILED,
    "OUT_OF_MEMORY": JobStatus.FAILED,
    "TIMEOUT": JobStatus.FAILED,
    # somebody or something ended it
    "CANCELLED": JobStatus.ABORTED,
    "PREEMPTED": JobStatus.ABORTED,
    "DEADLINE": JobStatus.ABORTED,
    "NODE_FAIL": JobStatus.ABORTED,
    "BOOT_FAIL": JobStatus.ABORTED,
    "REVOKED": JobStatus.ABORTED,
}

_ROW = re.compile(r"(?P<job>[^|\s]+)\|(?P<state>\S+).*")
# How sacct writes the tasks of an array still waiting: "7_[0-2,5%4]".
_TASKS = re.compile(r"(?P<array>\d+)_\[(?P<ranges>[^\]%]*)(?:%\d+)?\]")


def job_status(state: str) -> JobStatus:
    """Map a state as Slurm prints it to the common status."""
    return _STATES.get(state, JobStatus.UNKNOWN)


def parse_queue(result: CommandResult) -> dict[JobID, JobStatus]:
    """Read what ``squeue -o %i|%T`` printed.

    Args:
        result: The outcome of ``squeue``.

    Returns:
        The status of each task the controller knows.

    Raises:
        ValueError: ``squeue`` failed for another reason than not knowing a job.
    """
    # It says so only when it knows none of the jobs: no job is not a failure.
    if result.returncode != 0 and "Invalid job id" not in result.stderr:
        raise ValueError(failure(result, "squeue"))
    return {JobID(job): job_status(state) for job, state in _rows(result)}


def parse_accounting(
    result: CommandResult, ids: Sequence[JobID]
) -> dict[JobID, JobStatus]:
    """Read what ``sacct -o JobID,State`` printed about the arrays of ``ids``.

    Slurm drops a task cancelled while it waited from its array, leaving no row
    of its own, so its array must be looked at: a task that is missing from an
    array the accounting knows did not make it.

    Args:
        result: The outcome of ``sacct``.
        ids: The jobs that were asked about.

    Returns:
        The status of each of ``ids`` the accounting can tell about.

    Raises:
        ValueError: ``sacct`` failed for another reason than the site keeping
            no accounting, in which case it knows nothing.
    """
    if result.returncode != 0:
        if "accounting storage is disabled" in result.stderr:
            return {}
        raise ValueError(failure(result, "sacct"))

    known: dict[str, JobStatus] = {}
    arrays = set()
    for job, state in _rows(result):
        arrays.add(job.partition("_")[0])
        known.update(dict.fromkeys(_expand(job), job_status(state)))
    statuses = {}
    for job in ids:
        if job in known:
            statuses[job] = known[job]
        elif job.partition("_")[0] in arrays:
            statuses[job] = JobStatus.ABORTED
    return statuses


def _rows(result: CommandResult) -> Iterator[tuple[str, str]]:
    """Yield each row's job and state; a state may go on, as in "CANCELLED by 1000"."""
    for line in result.stdout.splitlines():
        if match := _ROW.fullmatch(line.strip()):
            yield match["job"], match["state"]


def _expand(job: str) -> Iterator[str]:
    """Yield the tasks a row stands for: one, or all those of a range like "7_[0-2,5]"."""
    match = _TASKS.fullmatch(job)
    if match is None:
        yield job
        return
    for part in match["ranges"].split(","):
        first, _, last = part.partition("-")
        for task in range(int(first), int(last or first) + 1):
            yield f"{match['array']}_{task}"
