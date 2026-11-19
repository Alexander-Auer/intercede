"""Job descriptions: a job written in ARC's ADL (the EMI-ES activity description).

ARC has no cluster of copies: every copy is a job of its own, with its own
description. What differs between copies (environment, secrets) is written in
that copy's description, and all of them go to the CE in one request.

ADL is XML, built here with ElementTree, so every value is escaped by the XML
writer instead of being quoted by hand. A description names the files the
client uploads, never their content: secrets reach the job's session directory
by upload, not through the description.

A description only says what to run and where its files go. It sets no
policy on the job's lifetime: no holds, no automatic removal, no retries.
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
_NOT_IN_XML = re.compile("[^\t\n\x20-퟿-�\U00010000-\U0010ffff]")

_BYTES_PER_MB = 2**20


@dataclass(frozen=True)
class InputFile:
    """A file the job finds in its session directory when it starts."""

    name: str  # relative to the session directory
    source: str | None = None  # a URL ARC fetches; None: uploaded by the client
    executable: bool = False


@dataclass(frozen=True)
class OutputFile:
    """A file the job leaves in its session directory."""

    name: str  # relative to the session directory; "/" is all of it
    target: str | None = None  # a URL ARC uploads it to; None: kept for the client


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
        _child(executable, "Argument", argument)

    _child(application, "Output", plan.stdout)
    _child(application, "Error", plan.stderr)

    for name, value in sorted(plan.environment.items()):
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a valid environment variable name")
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

    # Tells ARC to wait for the files the client uploads before it starts the job.
    if any(input_file.source is None for input_file in plan.inputs):
        _child(staging, "ClientDataPush", "true")

    for input_file in plan.inputs:
        _input_file(staging, input_file)

    kept = {output_file.name for output_file in plan.outputs}
    # ARC keeps for the client only what is listed, stdout and stderr included.
    streams = [
        OutputFile(name) for name in (plan.stdout, plan.stderr) if name not in kept
    ]
    for output_file in [*streams, *plan.outputs]:
        _output_file(staging, output_file)

    return staging


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
