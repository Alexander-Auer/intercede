"""From ARC's job states to ``JobStatus``.

Reading only: the state is the one ARC reports, never one interCEde sets on a
job. ARC's REST interface writes its states in capitals ("QUEUING"); they are
read whatever their case.

Ported from DIRAC's ``AREXComputingElement`` (``STATES_MAP``), with two
differences:

- HELD is waiting, not running. The batch system behind the CE holds the job,
  and can release it: the same job through the Slurm backend is waiting too.
  DIRAC also kills held jobs while it reads their state; that is a policy on
  the job's lifetime, which is the consumer's to apply.
- WIPED is unknown, not aborted. ARC has removed the job's files, so it no
  longer knows how the job ended.
"""

from __future__ import annotations

from collections.abc import Mapping

from intercede.models import JobStatus

_STATES: Mapping[str, JobStatus] = {
    # accepted, and on its way to the batch system
    "ACCEPTING": JobStatus.WAITING,
    "ACCEPTED": JobStatus.WAITING,
    "PREPARING": JobStatus.WAITING,
    "PREPARED": JobStatus.WAITING,
    "SUBMITTING": JobStatus.WAITING,
    "QUEUING": JobStatus.WAITING,
    "HELD": JobStatus.WAITING,
    # running, including the steps that end it
    "RUNNING": JobStatus.RUNNING,
    "OTHER": JobStatus.RUNNING,
    "EXITINGLRMS": JobStatus.RUNNING,
    "EXECUTED": JobStatus.RUNNING,
    "FINISHING": JobStatus.RUNNING,
    "FINISHED": JobStatus.DONE,
    "FAILED": JobStatus.FAILED,
    # somebody ended it
    "KILLING": JobStatus.ABORTED,
    "KILLED": JobStatus.ABORTED,
    # gone from the CE
    "WIPED": JobStatus.UNKNOWN,
}


def job_status(state: str) -> JobStatus:
    """Map a state as ARC reports it to the common status."""
    return _STATES.get(state.upper(), JobStatus.UNKNOWN)
