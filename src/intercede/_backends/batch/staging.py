"""A submission as files: what is refused, the script that starts a copy, what is staged.

Everything per copy is here, in the same way for every scheduler: a scheduler
only queues the runner once per copy (see ``layout``), so an environment or a
secret never reaches a job description. Being a file on the resource, the runner
needs a POSIX shell there, and no Python.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from intercede._backends.batch import layout
from intercede.exceptions import SpecificationRejectedError
from intercede.models import CopyVars, SubmissionSpec

_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The way up from a copy's working directory to its submission's.
_UP = "../../.."


def check(
    spec: SubmissionSpec,
    copies: Sequence[CopyVars],
    launcher: Sequence[str] | None,
    resource: str,
) -> None:
    """Refuse what the resource cannot honour, before anything is staged.

    Args:
        spec: What every copy shares.
        copies: What differs per copy.
        launcher: The configured container launcher, if any.
        resource: The resource, for the error messages.

    Raises:
        SpecificationRejectedError: ``spec`` or a copy cannot be honoured.
    """
    for index, copy in enumerate(copies):
        if clashes := spec.clashes_with(copy):
            raise SpecificationRejectedError(
                f"copy {index}: secrets {sorted(clashes)} are named like inputs"
            )
    # No data mover of its own: a batch system can only keep files for collection.
    for ref in spec.inputs:
        if "://" in ref.source:
            raise SpecificationRejectedError(
                f"input {ref.name!r}: {resource} cannot fetch inputs from URLs"
            )
    for member in spec.outputs.members if spec.outputs else ():
        if member.destination is not None:
            raise SpecificationRejectedError(
                f"output {member.pattern!r}: {resource} cannot upload outputs to URLs"
            )
    if spec.container is not None:
        if launcher is None:
            raise SpecificationRejectedError(
                f"{resource} has no container_launcher configured, so it cannot run "
                f"{spec.container.image!r}"
            )
        if Path(spec.executable).is_file():
            raise SpecificationRejectedError(
                "a container job runs a command inside the image, not a local file"
            )
    for environment in (spec.environment, *(copy.environment for copy in copies)):
        for name in environment:
            if not _VARIABLE.fullmatch(name):
                raise SpecificationRejectedError(
                    f"{name!r} is not the name of an environment variable"
                )


def runner(
    spec: SubmissionSpec, copies: Sequence[CopyVars], launcher: Sequence[str] | None
) -> str:
    """Write the script that starts one copy, given as its argument.

    It ends by replacing itself with the program, so what the scheduler sees
    exit is the program, and a job that failed reads as one.

    Args:
        spec: What every copy shares.
        copies: What differs per copy.
        launcher: The configured container launcher, if any.

    Returns:
        The script, to be run from the submission's directory.
    """
    lines = [
        "#!/bin/sh",
        f'cd "{layout.COPIES}/$1/{layout.WORK}" || exit 1',
    ]
    if spec.inputs:
        # Copied, not linked: a copy that changes an input changes its own.
        lines.append(f"cp -R {_UP}/{layout.INPUTS}/. . || exit 1")
    lines.extend(_exports(spec.environment))
    if any(copy.environment for copy in copies):
        lines.append('case "$1" in')
        for index, copy in enumerate(copies):
            if copy.environment:
                lines.append(f"{index})")
                lines.extend(_exports(copy.environment))
                lines.append(";;")
        lines.append("esac")
    for stream in (spec.stdout, spec.stderr):
        if stream and (parent := PurePosixPath(stream).parent) != PurePosixPath("."):
            lines.append(f"mkdir -p {shlex.quote(str(parent))} || exit 1")
    lines.append(f"exec {_command(spec, launcher)} {_redirections(spec)}")
    return "\n".join(lines) + "\n"


def stage(
    spec: SubmissionSpec,
    copies: Sequence[CopyVars],
    launcher: Sequence[str] | None,
    files: Mapping[str, str],
    directory: Path,
) -> None:
    """Build a submission's directory on this host, to be copied to the resource as it is.

    Inputs are read here and now: the caller may delete its files once ``submit``
    returns. Only this user can read the result, and a secret is 0600 from the
    moment it exists.

    Args:
        spec: What every copy shares.
        copies: What differs per copy.
        launcher: The configured container launcher, if any.
        files: The scheduler's files, by name in the directory.
        directory: Where to build it; must not exist yet.

    Raises:
        SpecificationRejectedError: The executable or an input cannot be read.
    """
    directory.mkdir(mode=0o700)
    _write(directory / layout.RUNNER, runner(spec, copies, launcher))
    for name, content in files.items():
        _write(directory / name, content)
    if Path(spec.executable).is_file():
        target = directory / layout.EXECUTABLE
        try:
            shutil.copyfile(spec.executable, target)
        except OSError as exc:
            raise SpecificationRejectedError(
                f"cannot read the executable {spec.executable}: {exc.strerror}"
            ) from exc
        target.chmod(0o700)  # whatever it was on this host
    for ref in spec.inputs:
        target = directory / layout.INPUTS / ref.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy(ref.source, target)
        except OSError as exc:
            raise SpecificationRejectedError(
                f"input {ref.name!r}: cannot read {ref.source}: {exc.strerror}"
            ) from exc
    for index, copy in enumerate(copies):
        work = directory / layout.copy_dir(index) / layout.WORK
        work.mkdir(parents=True, mode=0o700)
        for name, secret in copy.secrets.items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_private(target, secret.get_secret_value())


def _exports(environment: Mapping[str, str]) -> list[str]:
    return [
        f"export {name}={shlex.quote(value)}"
        for name, value in sorted(environment.items())
    ]


def _command(spec: SubmissionSpec, launcher: Sequence[str] | None) -> str:
    executable = (
        f"{_UP}/{layout.EXECUTABLE}"
        if Path(spec.executable).is_file()
        else shlex.quote(spec.executable)
    )
    arguments = [shlex.quote(argument) for argument in spec.arguments]
    if spec.container is None or launcher is None:
        return " ".join([executable, *arguments])
    image = spec.container.image
    start = [shlex.quote(part.replace("{image}", image)) for part in launcher]
    return " ".join([*start, executable, *arguments])


def _redirections(spec: SubmissionSpec) -> str:
    stdout = shlex.quote(spec.stdout) if spec.stdout else "../stdout"
    stderr = shlex.quote(spec.stderr) if spec.stderr else "../stderr"
    return f">{stdout} 2>&1" if stdout == stderr else f">{stdout} 2>{stderr}"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(path, content.encode())


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
