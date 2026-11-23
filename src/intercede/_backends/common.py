"""What every backend does the same way, written once.

Only what means exactly the same on every backend belongs here. What one
backend does its own way stays with that backend, even when it looks alike:
these are plain functions and values, never a base class.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from intercede.exceptions import SpecificationRejectedError
from intercede.models import CopyVars, FileRef, JobStatus, SubmissionSpec

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


def is_url(source: str) -> bool:
    """Tell whether an input's source is a URL the resource fetches, not a local path."""
    return "://" in source


def read_input(ref: FileRef) -> bytes:
    """Read a local input now: the caller may delete its file once ``submit`` returns.

    Raises:
        SpecificationRejectedError: The file cannot be read.
    """
    try:
        return Path(ref.source).read_bytes()
    except OSError as exc:
        raise SpecificationRejectedError(
            f"input {ref.name!r}: cannot read {ref.source}: {exc.strerror}"
        ) from exc


def check_container(
    spec: SubmissionSpec, launcher: Sequence[str] | None, resource: str
) -> None:
    """Refuse a container image the resource cannot run, before anything is staged.

    Args:
        spec: What every copy shares.
        launcher: The configured container launcher, if any.
        resource: The resource, for the error messages.

    Raises:
        SpecificationRejectedError: The spec asks for an image but no launcher
            is configured, or its executable is a file on this host: a
            container job runs a command inside the image.
    """
    if spec.container is None:
        return
    if launcher is None:
        raise SpecificationRejectedError(
            f"{resource} has no container_launcher configured, so it cannot run "
            f"{spec.container.image!r}"
        )
    if Path(spec.executable).is_file():
        raise SpecificationRejectedError(
            "a container job runs a command inside the image, not a local file"
        )


def container_command(spec: SubmissionSpec, launcher: Sequence[str]) -> list[str]:
    """Return the argv that runs the executable inside the spec's image.

    Args:
        spec: A spec with a container image, accepted by ``check_container``.
        launcher: The configured launcher; ``{image}`` is replaced by the image.

    Returns:
        The launcher, then the executable and its arguments.
    """
    assert spec.container is not None  # check_container() was called first
    image = spec.container.image
    start = [part.replace("{image}", image) for part in launcher]
    return [*start, spec.executable, *spec.arguments]
