"""What every backend does the same way, written once.

Only what means exactly the same on every backend belongs here. What one
backend does its own way stays with that backend, even when it looks alike:
these are plain functions and values, never a base class.
"""

from __future__ import annotations

from collections.abc import Sequence

from intercede.exceptions import SpecificationRejectedError
from intercede.models import CopyVars, JobStatus, SubmissionSpec

# A job in one of these can still be cancelled; in any other it has finished,
# or the backend no longer knows it.
ACTIVE = frozenset({JobStatus.SUBMITTED, JobStatus.WAITING, JobStatus.RUNNING})


def copies_of(spec: SubmissionSpec, copies: int | Sequence[CopyVars]) -> list[CopyVars]:
    """Return what each copy of a submission gets, as ``submit`` was given it.

    Args:
        spec: What every copy shares.
        copies: A count of identical copies, or one ``CopyVars`` per copy.

    Returns:
        One ``CopyVars`` per copy, in copy order.

    Raises:
        SpecificationRejectedError: A copy has a secret named like one of the
            spec's inputs; both would be staged to the same file.
    """
    copy_vars = [CopyVars()] * copies if isinstance(copies, int) else list(copies)
    for index, copy in enumerate(copy_vars):
        if clashes := spec.clashes_with(copy):
            raise SpecificationRejectedError(
                f"copy {index}: secrets {sorted(clashes)} are named like inputs"
            )
    return copy_vars
