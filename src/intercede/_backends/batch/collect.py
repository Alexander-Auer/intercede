"""Deciding what to collect of a job's output, from a listing of its directory.

Everything is decided from that listing, before a single byte is copied, as the
names and sizes come from a payload interCEde never inspects: only what the
specification asked for is copied, within the configured limits, and never to a
place outside the destination.

The listing uses GNU find's ``-printf``, so it needs a Linux resource.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from intercede._backends.batch import layout
from intercede._backends.batch.job import Manifest


@dataclass(frozen=True)
class Entry:
    """One file in a copy's directory, as the listing shows it."""

    name: str  # relative to the copy's directory
    size: int
    is_link: bool


@dataclass(frozen=True)
class Selection:
    """What to copy of one job, by name in the copy's directory."""

    stdout: str | None = None
    stderr: str | None = None
    log: str | None = None
    files: tuple[str, ...] = ()  # payload members only


def listing_command(directory: str) -> list[str]:
    """List a copy's files and links with their sizes, not following any link."""
    return [
        "find",
        directory,
        "(",
        "-type",
        "f",
        "-o",
        "-type",
        "l",
        ")",
        "-printf",
        "%y\\t%s\\t%P\\0",
    ]


def parse_listing(raw: str) -> list[Entry]:
    """Read what ``listing_command`` printed.

    Args:
        raw: Its output: a type, a size and a name per file, each ending in NUL,
            the one character a name cannot hold.

    Returns:
        The entries.

    Raises:
        ValueError: The output cannot be read.
    """
    entries = []
    for record in raw.split("\0")[:-1]:
        kind, size, name = record.split("\t", 2)
        entries.append(Entry(name=name, size=int(size), is_link=kind == "l"))
    return entries


def select(
    entries: Sequence[Entry],
    manifest: Manifest,
    *,
    max_files: int,
    max_bytes: int,
) -> Selection:
    """Choose what to copy of a job, and refuse what is not allowed.

    Args:
        entries: The copy's directory, as listed.
        manifest: What the job's specification asked for.
        max_files: How many files may be collected.
        max_bytes: How many bytes may be collected.

    Returns:
        What to copy.

    Raises:
        ValueError: The reason the job's output cannot be collected.
    """
    stdout = stderr = log = None
    files = []
    chosen = []
    prefix = f"{layout.WORK}/"
    for entry in entries:
        if entry.name == manifest.stdout:
            stdout = entry.name
        elif entry.name == manifest.stderr:
            stderr = entry.name
        elif entry.name == layout.LOG:
            log = entry.name
        elif entry.name.startswith(prefix) and _wanted(
            entry.name.removeprefix(prefix), manifest
        ):
            files.append(entry.name)
        else:
            continue
        chosen.append(entry)

    for entry in chosen:
        path = PurePosixPath(entry.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("the output holds a path outside the job's directory")
        if entry.is_link:
            raise ValueError(f"the output holds a link: {entry.name}")
    if len(chosen) > max_files:
        raise ValueError(f"more than {max_files} output files")
    if sum(entry.size for entry in chosen) > max_bytes:
        raise ValueError(f"output larger than {max_bytes} bytes")
    return Selection(stdout, stderr, log, tuple(sorted(files)))


def _wanted(name: str, manifest: Manifest) -> bool:
    """Tell whether a file in the working directory is output the job's specification asked for."""
    for member in manifest.members:
        if name == member or name.startswith(f"{member}/"):
            return True  # by name: even a staged file, if that is what is asked for
    if name in manifest.staged or any(
        name.startswith(f"{staged}/") for staged in manifest.staged
    ):
        return False
    return manifest.include_unlisted or any(
        fnmatchcase(name, member) for member in manifest.members
    )
