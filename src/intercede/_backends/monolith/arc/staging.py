"""What each copy's description says, and what is uploaded to its session directory.

A job's session directory is its working directory. ARC creates it from the
description; the backend then uploads every file the description names without
a source: a local executable, the local inputs and the copy's secrets. Their
content is read before the CE is contacted, so the caller may delete its files
once ``submit`` returns, and an unreadable file fails the submission before
any job exists.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from intercede._backends._arc.description import InputFile, JobPlan, OutputFile
from intercede._backends.common import (
    check_container,
    container_command,
    is_url,
    read_input,
)
from intercede.exceptions import SpecificationRejectedError
from intercede.models import CopyVars, SubmissionSpec

# Names in the session directory the backend uses itself. Hidden, so a payload
# that lists its working directory does not trip over them.
EXECUTABLE = ".intercede-executable"
STDOUT = ".intercede-stdout"
STDERR = ".intercede-stderr"
_RESERVED = frozenset({EXECUTABLE, STDOUT, STDERR})

_GLOB = re.compile(r"[*?\[]")


def check(
    spec: SubmissionSpec,
    copies: Sequence[CopyVars],
    launcher: Sequence[str] | None,
    resource: str,
) -> None:
    """Refuse what the CE cannot honour, before any file is read.

    Args:
        spec: What every copy shares.
        copies: What differs per copy.
        launcher: The configured container launcher, if any.
        resource: The CE, for the error messages.

    Raises:
        SpecificationRejectedError: ``spec`` or a copy cannot be honoured.
    """
    check_container(spec, launcher, resource)
    outputs = spec.outputs
    if outputs is not None and outputs.include_unlisted:
        raise SpecificationRejectedError(
            f"{resource}: this backend does not collect outputs, "
            "so it cannot keep unlisted ones"
        )
    for member in outputs.members if outputs else ():
        if member.destination is None:
            raise SpecificationRejectedError(
                f"output {member.pattern!r}: this backend does not collect outputs, "
                "so it needs a destination"
            )
        if _GLOB.search(member.pattern):
            raise SpecificationRejectedError(
                f"output {member.pattern!r}: ARC uploads an output by its name, "
                "not by pattern"
            )
    names = {ref.name for ref in spec.inputs}
    names.update(name for copy in copies for name in copy.secrets)
    if taken := sorted(names & _RESERVED):
        raise SpecificationRejectedError(
            f"{taken} are names the ARC backend uses itself in the job's directory"
        )


def uploads(spec: SubmissionSpec) -> dict[str, bytes]:
    """Read what every copy uploads: a local executable and the local inputs.

    Returns:
        The content of each file, by its name in the session directory.

    Raises:
        SpecificationRejectedError: The executable or an input cannot be read.
    """
    files: dict[str, bytes] = {}
    if _is_local(spec):
        try:
            files[EXECUTABLE] = Path(spec.executable).read_bytes()
        except OSError as exc:
            raise SpecificationRejectedError(
                f"cannot read the executable {spec.executable}: {exc.strerror}"
            ) from exc
    for ref in spec.inputs:
        if not is_url(ref.source):
            files[ref.name] = read_input(ref)
    return files


def plans(
    spec: SubmissionSpec,
    copies: Sequence[CopyVars],
    launcher: Sequence[str] | None,
    queue: str | None,
) -> list[JobPlan]:
    """Write one plan per copy, for a spec ``check`` accepted.

    Args:
        spec: What every copy shares.
        copies: What differs per copy.
        launcher: The configured container launcher, if any.
        queue: The queue when the spec names none: the configured one, or
            None for the CE's default.

    Returns:
        One plan per copy, in copy order.
    """
    local = _is_local(spec)
    if spec.container is not None and launcher is not None:
        executable, *arguments = container_command(spec, launcher)
    else:
        executable = EXECUTABLE if local else spec.executable
        arguments = list(spec.arguments)

    shared = [InputFile(EXECUTABLE, executable=True)] if local else []
    shared += [
        # A URL is fetched by the CE; anything else is uploaded.
        InputFile(ref.name, source=ref.source if is_url(ref.source) else None)
        for ref in spec.inputs
    ]
    outputs = [
        OutputFile(member.pattern, target=member.destination)
        for member in (spec.outputs.members if spec.outputs else ())
    ]
    return [
        JobPlan(
            executable=executable,
            arguments=arguments,
            environment={**spec.environment, **copy.environment},
            inputs=[*shared, *(InputFile(name) for name in copy.secrets)],
            outputs=outputs,
            stdout=spec.stdout or STDOUT,
            stderr=spec.stderr or STDERR,
            cpus=spec.resources.cpus,
            memory_mb=spec.resources.memory_mb,
            wall_time_s=spec.resources.wall_time_s,
            queue=spec.resources.queue or queue,
        )
        for copy in copies
    ]


def _is_local(spec: SubmissionSpec) -> bool:
    """Tell whether the executable is a file on this host, to be uploaded with the job.

    Never for a container job: ``check_container`` refuses a local file there.
    """
    return Path(spec.executable).is_file()
