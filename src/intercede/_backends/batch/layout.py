"""Where a submission's files are on the resource.

Every submission has a directory of its own, in the configured working area::

    <workdir>/<submission>/
        run.sh                  starts one copy: ``sh run.sh <copy>``, from this directory
        executable              the program, when it was shipped from the consumer's host
        inputs/                 what every copy starts with
        copies/<copy>/
            log                 what the scheduler has to say about this copy
            stdout, stderr      the copy's streams, unless the specification names them
            work/               the copy's working directory: inputs, secrets, what it writes

A scheduler only needs ``RUNNER`` and ``scheduler_log``: its job starts the
runner for each copy from the submission's directory, and keeps its log there.
A scheduler that moves a job's files itself also needs ``shared``: what every
copy's job needs from the submission, besides its own copy's directory.
"""

from __future__ import annotations

from pathlib import Path

from intercede.models import SubmissionSpec

RUNNER = "run.sh"
EXECUTABLE = "executable"
INPUTS = "inputs"
COPIES = "copies"
LOG = "log"
WORK = "work"


def copy_dir(copy: int | str) -> str:
    """Return a copy's directory, relative to the submission's."""
    return f"{COPIES}/{copy}"


def ships_executable(spec: SubmissionSpec) -> bool:
    """Tell whether the program is a file of the consumer's host, staged as ``EXECUTABLE``."""
    return Path(spec.executable).is_file()


def shared(spec: SubmissionSpec) -> list[str]:
    """What every copy's job needs from the submission's directory, besides its copy's.

    Each name is relative to the submission's directory. A copy's own directory
    (``copy_dir``) is not shared: it holds that copy's secrets.
    """
    names = [RUNNER]
    if ships_executable(spec):
        names.append(EXECUTABLE)
    if spec.inputs:
        names.append(INPUTS)
    return names


def scheduler_log(copy: int | str) -> str:
    """Return where the scheduler logs about a copy, relative to the submission's directory.

    Args:
        copy: The copy's index, or the scheduler's placeholder for it.
    """
    return f"{copy_dir(copy)}/{LOG}"
