"""The argv of each Slurm tool, and the job IDs ``sbatch`` gives.

A job is one task of an array, so its ID is ``<array>_<task>`` and task ``i`` is
copy ``i``. ``sbatch --parsable --array=0-2`` only prints the array's ID.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from intercede._backends.batch.protocols import CommandResult
from intercede.models import JobID

_JOB_ID = re.compile(r"\d+_\d+")
_ARRAY_ID = re.compile(r"(?P<array>\d+)(?:;\S+)?")  # "<id>" or "<id>;<cluster>"


def is_job_id(value: str) -> bool:
    """Tell whether ``value`` has the form of the job IDs ``parse_submit`` returns."""
    return _JOB_ID.fullmatch(value) is not None


def submit(script: str, workdir: str) -> list[str]:
    """Queue a job script, to run from ``workdir``; prints the array's ID and nothing else."""
    return ["sbatch", "--parsable", f"--chdir={workdir}", script]


def query(ids: Sequence[JobID]) -> list[str]:
    """Ask the controller about jobs, one row per task, whatever their state."""
    return ["squeue", "-h", "-r", "-t", "all", "-j", ",".join(ids), "-o", "%i|%T"]


def accounting(ids: Sequence[JobID]) -> list[str]:
    """Ask the accounting about the whole arrays of jobs.

    The arrays and not the tasks, since a task cancelled while it waited leaves
    no row of its own (see ``status.parse_accounting``).
    """
    arrays = dict.fromkeys(job.partition("_")[0] for job in ids)
    return ["sacct", "-n", "-X", "-P", "-j", ",".join(arrays), "-o", "JobID,State"]


def cancel(ids: Sequence[JobID]) -> list[str]:
    """Cancel jobs."""
    return ["scancel", *ids]


def parse_submit(result: CommandResult, count: int) -> list[JobID]:
    """Return one job ID per copy, from what ``sbatch --parsable`` printed.

    Args:
        result: The outcome of ``sbatch``.
        count: How many tasks the job script's array has.

    Returns:
        The tasks' job IDs, in copy order.

    Raises:
        ValueError: ``sbatch`` failed, or its output cannot be read.
    """
    if result.returncode != 0:
        raise ValueError(failure(result, "sbatch"))
    # The last line: a site's login scripts may print before the command runs.
    lines = result.stdout.strip().splitlines()
    match = _ARRAY_ID.fullmatch(lines[-1].strip()) if lines else None
    if match is None:
        raise ValueError(f"unexpected sbatch output: {result.stdout.strip()!r}")
    return [JobID(f"{match['array']}_{task}") for task in range(count)]


def failure(result: CommandResult, tool: str) -> str:
    """Describe why a tool failed, in one line."""
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return f"{tool} failed: {lines[-1] if lines else f'exit code {result.returncode}'}"
