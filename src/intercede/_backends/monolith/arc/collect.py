"""Deciding what to collect of a job's output, from a listing of its session directory.

Everything is decided from the listing and from the job's description before a
single byte is copied, because the names come from a payload interCEde never
inspects. Only what the specification asked for is copied, no more files than
configured, and never to a place outside the destination. A-REX lists neither
sizes nor links: the bytes are counted while they arrive, and a link is never
listed, so it is never collected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from intercede._backends._arc.description import Kept


@dataclass(frozen=True)
class Selection:
    """What to copy of one job, by name in its session directory."""

    stdout: str | None = None
    stderr: str | None = None
    files: tuple[str, ...] = ()  # payload members only

    def __len__(self) -> int:
        streams = (self.stdout, self.stderr)
        return len(self.files) + sum(name is not None for name in streams)


def child(directory: str, name: str) -> str:
    """Join a name from a listing of ``directory`` to it.

    Args:
        directory: Relative to the session directory: empty, or ending in "/".
        name: As the listing gave it.

    Returns:
        The name relative to the session directory.

    Raises:
        ValueError: ``name`` is not a single name in that directory.
    """
    if not name or "/" in name or "\0" in name or name in {".", ".."}:
        raise ValueError("the output holds a path outside the job's directory")
    return directory + name


def select(names: Sequence[str], kept: Kept, *, max_files: int) -> Selection:
    """Choose what to copy of a job, and refuse what is not allowed.

    Args:
        names: Every file in the session directory, relative to it.
        kept: What the job's description keeps.
        max_files: How many files may be collected.

    Returns:
        What to copy.

    Raises:
        ValueError: The reason the job's output cannot be collected.
    """
    stdout = kept.stdout if kept.stdout in names else None
    stderr = kept.stderr if kept.stderr in names else None
    streams = {kept.stdout, kept.stderr}
    files = sorted(
        name for name in names if name not in streams and _wanted(name, kept)
    )
    selection = Selection(stdout, stderr, tuple(files))
    if len(selection) > max_files:
        raise ValueError(f"more than {max_files} output files")
    return selection


def _wanted(name: str, kept: Kept) -> bool:
    """Tell whether a file is output the job's specification asked for."""
    for member in kept.members:
        if name == member or name.startswith(f"{member}/"):
            return True  # by name: even a staged file, if that is what is asked for
    if any(name == staged or name.startswith(f"{staged}/") for staged in kept.staged):
        return False
    return kept.include_unlisted or any(
        fnmatchcase(name, member) for member in kept.members
    )
