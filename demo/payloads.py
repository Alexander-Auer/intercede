"""Ready-made payloads for the job board: one specification each, the same for every backend.

Each payload is a shell script on this host. A local executable is shipped with
the job by every backend, so the payload depends on nothing installed where it
runs. ``fake`` is what the Fake backend plays instead, since nothing runs there:
a timeline of states, which ``FakeDriver`` applies with the fake's test hooks.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

from intercede.models import CopyVars, OutputMember, OutputSpec, SubmissionSpec


@dataclass(frozen=True)
class FakeStep:
    """After ``after`` seconds, the fake job is in ``status`` with this output so far."""

    after: float
    status: str
    stdout: str = ""
    files: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class Payload:
    name: str
    title: str
    blurb: str
    script: str
    outputs: tuple[str, ...] = ()
    fake: tuple[FakeStep, ...] = ()


def _progress_steps() -> tuple[FakeStep, ...]:
    steps = [FakeStep(1, "Waiting")]
    lines = ""
    for i in range(1, 31):
        lines += f"step {i}/30\n"
        steps.append(FakeStep(1 + i, "Running", lines))
    steps.append(FakeStep(32, "Done", lines + "finished\n"))
    return tuple(steps)


PAYLOADS: dict[str, Payload] = {
    payload.name: payload
    for payload in (
        Payload(
            "quick",
            "Quick job",
            "Says hello from the worker, and shows its secret arrived as a private file.",
            'echo "hello from $(hostname), copy $COPY_INDEX"\n'
            "stat -c 'pilot.secret: %A, %s bytes' pilot.secret\n",
            fake=(
                FakeStep(1, "Waiting"),
                FakeStep(2, "Running"),
                FakeStep(
                    4,
                    "Done",
                    "hello from fake, copy $COPY_INDEX\npilot.secret: -rw-------, 32 bytes\n",
                ),
            ),
        ),
        Payload(
            "progress",
            "30 s with progress",
            "Prints a line a second: read it while it runs, if the backend is Diagnosable.",
            'for i in $(seq 1 30); do echo "step $i/30"; sleep 1; done\necho finished\n',
            fake=_progress_steps(),
        ),
        Payload(
            "fail",
            "Failing job",
            "Writes to stderr and exits 3: the job fails, the submission did not.",
            'echo "about to fail"\necho "something went wrong" >&2\nexit 3\n',
            fake=(
                FakeStep(1, "Waiting"),
                FakeStep(2, "Running"),
                FakeStep(4, "Failed", "about to fail\n"),
            ),
        ),
        Payload(
            "files",
            "Several output files",
            "Writes three result files; get_output collects them by name.",
            'for n in 1 2 3; do echo "result $n of copy $COPY_INDEX" > "result-$n.txt"; done\necho wrote 3 files\n',
            # By name, not "result-*.txt": HTCondor-CE refuses a pattern (scene 3 shows it).
            outputs=("result-1.txt", "result-2.txt", "result-3.txt"),
            fake=(
                FakeStep(1, "Waiting"),
                FakeStep(2, "Running"),
                FakeStep(
                    4,
                    "Done",
                    "wrote 3 files\n",
                    {
                        f"result-{n}.txt": f"result {n} of copy $COPY_INDEX\n".encode()
                        for n in (1, 2, 3)
                    },
                ),
            ),
        ),
    )
}


def write_scripts(directory: Path) -> dict[str, Path]:
    """Write every payload's script into ``directory``; return their paths by name."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for payload in PAYLOADS.values():
        path = directory / f"{payload.name}.sh"
        path.write_text(f"#!/bin/sh\n{payload.script}")
        path.chmod(0o755)
        paths[payload.name] = path
    return paths


def specification(payload: Payload, script: Path) -> SubmissionSpec:
    """The specification every backend gets for ``payload``."""
    outputs = (
        OutputSpec(members=[OutputMember(pattern=p) for p in payload.outputs])
        if payload.outputs
        else None
    )
    return SubmissionSpec(
        executable=str(script), outputs=outputs, tag=f"demo:{payload.name}"
    )


def copies(count: int) -> list[CopyVars]:
    """One copy each: its own index as an identifier, and its own secret as a file."""
    return [
        CopyVars(
            environment={"COPY_INDEX": str(index)},
            secrets={"pilot.secret": secrets.token_hex(16).encode()},
        )
        for index in range(count)
    ]
