"""Job descriptions: a job written in ARC's ADL (the EMI-ES activity description).

ARC has no cluster of copies: every copy is a job of its own, with its own
description. What differs between copies (environment, secrets) is written in
that copy's description, and all of them go to the CE in one request.

ADL is XML, built here with ElementTree, so every value is escaped by the XML
writer instead of being quoted by hand. A description names the files the
client uploads, never their content: secrets reach the job's session directory
by upload, not through the description.

ARC's job script, which starts the executable on the worker, still runs each
argument through the shell: a "$" or a backtick in one would be expanded. It
also drops line breaks, from arguments and variables alike. Nothing written in
a description keeps such values as they are, so they are refused.

A description only says what to run and where its files go. It sets no
policy on the job's lifetime: no holds, no automatic removal, no retries.

ARC keeps the description of a job as it was sent, so it is also where the
backend reads back what a job keeps for collection (``read_kept``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from xml.etree import ElementTree

ADL_NAMESPACE = "http://www.eu-emi.eu/es/2010/12/adl"

# What a shell accepts as a variable name: ARC exports each one in the job script.
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Characters XML 1.0 cannot hold. A carriage return is refused as well: an XML
# parser turns it into a line feed, so it would not arrive as written.
_NOT_IN_XML = re.compile(r"[^\t\n\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")

# What ARC's job script changes in an argument, whatever the description says
# (ARC 7.2): it expands "$" and backticks, and drops line breaks. It escapes
# quotes and backslashes itself, so no escaping here could keep these.
_EXPANDED = re.compile(r"[$`\n]")

_BYTES_PER_MB = 2**20

# An output named so keeps all of the session directory.
EVERYTHING = "/"
_GLOB = re.compile(r"[*?\[]")


def is_glob(name: str) -> bool:
    """Tell whether an output name is a pattern; ARC reads every name literally."""
    return _GLOB.search(name) is not None


@dataclass(frozen=True)
class InputFile:
    """A file the job finds in its session directory when it starts."""

    name: str  # relative to the session directory
    source: str | None = None  # a URL ARC fetches; None: uploaded by the client
    executable: bool = False


@dataclass(frozen=True)
class OutputFile:
    """A file the job leaves in its session directory."""

    name: str  # relative to the session directory: a name, or a pattern if kept
    target: str | None = None  # a URL ARC uploads it to; None: kept for the client


@dataclass(frozen=True)
class Kept:
    """What a job keeps for collection, read back from its description."""

    stdout: str
    stderr: str
    members: tuple[str, ...] = ()  # names and patterns in the session directory
    include_unlisted: bool = False
    staged: tuple[str, ...] = ()  # inputs, the executable and secrets


@dataclass(frozen=True)
class JobPlan:
    """Everything one copy's description says, resolved to ARC's terms."""

    executable: str  # a file in the session directory, or a path on the worker
    stdout: str  # relative to the session directory, and always kept
    stderr: str
    arguments: Sequence[str] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    inputs: Sequence[InputFile] = ()
    outputs: Sequence[OutputFile] = ()
    include_unlisted: bool = False  # keep every file the job leaves, inputs aside
    cpus: int = 1
    memory_mb: int | None = None
    wall_time_s: int | None = None
    queue: str | None = None


def render(plans: Sequence[JobPlan]) -> str:
    """Write ``plans`` as the body of one request that creates a job per plan.

    Args:
        plans: One plan per copy, in copy order.

    Returns:
        One ``ActivityDescription``, or several inside ``ActivityDescriptions``.
        ARC answers with one entry per description, in the same order.

    Raises:
        ValueError: There is no plan, or a value cannot be written in ADL.
    """
    if not plans:
        raise ValueError("A request needs at least one job description")

    descriptions = [_description(plan) for plan in plans]
    if len(descriptions) == 1:
        root = descriptions[0]
    else:
        root = ElementTree.Element("ActivityDescriptions")
        root.extend(descriptions)

    # No XML declaration: ARC refuses a body that starts with one.
    return ElementTree.tostring(root, encoding="unicode")


def _description(plan: JobPlan) -> ElementTree.Element:
    # The namespace is declared once, as the default: every child inherits it.
    description = ElementTree.Element("ActivityDescription", xmlns=ADL_NAMESPACE)
    description.append(_application(plan))
    description.append(_resources(plan))
    description.append(_data_staging(plan))
    return description


def _application(plan: JobPlan) -> ElementTree.Element:
    application = ElementTree.Element("Application")

    executable = _child(application, "Executable")
    _child(executable, "Path", plan.executable)
    for argument in plan.arguments:
        if match := _EXPANDED.search(argument):
            raise ValueError(
                f"an argument cannot hold {match.group()!r}: "
                "ARC's job script would expand or drop it"
            )
        _child(executable, "Argument", argument)

    _child(application, "Output", plan.stdout)
    _child(application, "Error", plan.stderr)

    for name, value in sorted(plan.environment.items()):
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a valid environment variable name")
        if "\n" in value:
            raise ValueError(
                f"variable {name!r} cannot hold a line break: ARC's job script drops it"
            )
        variable = _child(application, "Environment")
        _child(variable, "Name", name)
        _child(variable, "Value", value)

    return application


def _resources(plan: JobPlan) -> ElementTree.Element:
    resources = ElementTree.Element("Resources")

    if plan.memory_mb is not None:
        # ADL counts bytes; ARC hands the batch system megabytes again.
        memory = str(plan.memory_mb * _BYTES_PER_MB)
        _child(resources, "IndividualPhysicalMemory", memory)

    # All of a job's slots on one node, as cpus are on every other backend.
    slots = _child(resources, "SlotRequirement")
    _child(slots, "NumberOfSlots", str(plan.cpus))
    _child(slots, "SlotsPerHost", str(plan.cpus))

    if plan.queue is not None:
        _child(resources, "QueueName", plan.queue)

    if plan.wall_time_s is not None:
        _child(resources, "WallTime", str(plan.wall_time_s))

    return resources


def _data_staging(plan: JobPlan) -> ElementTree.Element:
    staging = ElementTree.Element("DataStaging")

    # ARC starts the job once every input without a source has been uploaded.
    # Not ClientDataPush: with it, A-REX waits for a signal REST cannot send.
    for input_file in plan.inputs:
        _input_file(staging, input_file)

    for output_file in _outputs(plan):
        _output_file(staging, output_file)

    return staging


def _outputs(plan: JobPlan) -> list[OutputFile]:
    """Return the outputs a description lists, in a way ``read_kept`` reads back.

    ARC keeps for the client only what is listed, stdout and stderr included,
    and reads every name literally: a pattern keeps nothing. So a job that keeps
    a pattern, or everything unlisted, keeps all of its session directory ("/"),
    and the backend chooses at collection. Its patterns are listed as well, for
    that choice: ARC finds no file of such a name, which does the job no harm.
    With unlisted files kept, a pattern adds nothing and is left out, so a
    description with "/" and no pattern is one that keeps everything unlisted.
    """
    kept = [file for file in plan.outputs if file.target is None]
    if plan.include_unlisted:
        kept = [file for file in kept if not is_glob(file.name)]
    names = {file.name for file in kept}
    streams = [OutputFile(name) for name in (plan.stdout, plan.stderr)]
    everything = plan.include_unlisted or any(is_glob(name) for name in names)
    return [
        *(file for file in streams if file.name not in names),
        *kept,
        *([OutputFile(EVERYTHING)] if everything else []),
        *(file for file in plan.outputs if file.target is not None),
    ]


def read_kept(raw: str) -> Kept:
    """Read what a job keeps for collection from the description ARC kept of it.

    Args:
        raw: The description, as A-REX returns it.

    Returns:
        The job's streams, the outputs it keeps, and the files it was given.

    Raises:
        ValueError: The description is not one this module wrote.
    """
    try:
        root = ElementTree.fromstring(raw)  # noqa: S314  (from the CE we are trusting)
    except ElementTree.ParseError as exc:
        raise ValueError(f"the job's description cannot be read: {exc}") from None
    tag = f"{{{ADL_NAMESPACE}}}"
    stdout = root.findtext(f"{tag}Application/{tag}Output")
    stderr = root.findtext(f"{tag}Application/{tag}Error")
    if root.tag != f"{tag}ActivityDescription" or not stdout or not stderr:
        raise ValueError("the job's description is not one interCEde wrote")
    staging = f"{tag}DataStaging/"
    kept = [
        entry.findtext(f"{tag}Name", "")
        for entry in root.iterfind(f"{staging}{tag}OutputFile")
        if entry.find(f"{tag}Target") is None
    ]
    members = tuple(
        name for name in kept if name and name not in {EVERYTHING, stdout, stderr}
    )
    return Kept(
        stdout=stdout,
        stderr=stderr,
        members=members,
        include_unlisted=EVERYTHING in kept and not any(map(is_glob, members)),
        staged=tuple(
            entry.findtext(f"{tag}Name", "")
            for entry in root.iterfind(f"{staging}{tag}InputFile")
        ),
    )


def _input_file(staging: ElementTree.Element, file: InputFile) -> None:
    element = _child(staging, "InputFile")
    _child(element, "Name", file.name)

    if file.source is not None:
        source = _child(element, "Source")
        _child(source, "URI", file.source)

    if file.executable:
        _child(element, "IsExecutable", "true")


def _output_file(staging: ElementTree.Element, file: OutputFile) -> None:
    element = _child(staging, "OutputFile")
    _child(element, "Name", file.name)

    if file.target is not None:
        target = _child(element, "Target")
        _child(target, "URI", file.target)
        # Otherwise a failed upload leaves the job "finished" without its file.
        _child(target, "Mandatory", "true")


def _child(
    parent: ElementTree.Element, tag: str, text: str | None = None
) -> ElementTree.Element:
    element = ElementTree.SubElement(parent, tag)
    if text is not None:
        if match := _NOT_IN_XML.search(text):
            raise ValueError(
                f"{tag}: character {match.group()!r} cannot be written in a job description"
            )
        element.text = text
    return element
