"""The job script: a ``SubmissionSpec``'s resources in Slurm's terms.

Copies are one job array, so ``count`` copies are one ``sbatch``. What differs
between copies is the runner's business, which takes the copy from the task index.
"""

from __future__ import annotations

import re

from intercede._backends.batch import layout
from intercede.exceptions import SpecificationRejectedError
from intercede.models import Resources

_PARTITION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def render(resources: Resources, count: int, default_queue: str | None) -> str:
    """Write the job script of a submission.

    Args:
        resources: What each copy asks of the resource.
        count: How many copies to queue, as tasks of one array.
        default_queue: The partition to use when ``resources`` names none.

    Returns:
        The script, to be run from the submission's directory.

    Raises:
        SpecificationRejectedError: The partition's name cannot be a partition's.
    """
    lines = [
        "#!/bin/sh",
        f"#SBATCH --array=0-{count - 1}",
        f"#SBATCH --output={layout.scheduler_log('%a')}",
        # Not the environment of whoever submits: it would differ per transport,
        # and a local one may hold the consumer's secrets.
        "#SBATCH --export=NONE",
        f"#SBATCH --cpus-per-task={resources.cpus}",
    ]
    if resources.memory_mb is not None:
        lines.append(f"#SBATCH --mem={resources.memory_mb}")
    if resources.wall_time_s is not None:
        lines.append(f"#SBATCH --time={_duration(resources.wall_time_s)}")
    if (queue := resources.queue or default_queue) is not None:
        if not _PARTITION.fullmatch(queue):
            raise SpecificationRejectedError(
                f"{queue!r} cannot be the name of a Slurm partition"
            )
        lines.append(f"#SBATCH --partition={queue}")
    lines.append(f'exec sh {layout.RUNNER} "$SLURM_ARRAY_TASK_ID"')
    return "\n".join(lines) + "\n"


def _duration(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02}:{seconds % 60:02}"
