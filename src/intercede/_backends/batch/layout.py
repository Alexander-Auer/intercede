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
"""

from __future__ import annotations

RUNNER = "run.sh"
EXECUTABLE = "executable"
INPUTS = "inputs"
COPIES = "copies"
LOG = "log"
WORK = "work"


def copy_dir(copy: int | str) -> str:
    """Return a copy's directory, relative to the submission's."""
    return f"{COPIES}/{copy}"


def scheduler_log(copy: int | str) -> str:
    """Return where the scheduler logs about a copy, relative to the submission's directory.

    Args:
        copy: The copy's index, or the scheduler's placeholder for it.
    """
    return f"{copy_dir(copy)}/{LOG}"
