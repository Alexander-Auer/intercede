"""A second scheduler, for the tests only, which shows the backend is not shaped around Slurm.

It has no queue and no daemon: its job file starts each copy, one after the other,
during the submit command, and leaves the exit code next to it.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from typing import Any, Self

from intercede._backends.batch.protocols import (
    Command,
    CommandResult,
    SchedulerStatusMap,
    SubmitCommand,
)
from intercede.models import JobID, JobStatus, SubmissionSpec

_START = """#!/bin/sh
cd "$1" || exit 1
copy=0
while [ "$copy" -lt "$2" ]; do
    sh run.sh "$copy" >"copies/$copy/log" 2>&1
    echo $? >"copies/$copy/exit"
    echo "$PWD/copies/$copy/exit"
    copy=$((copy + 1))
done
"""


class Direct:
    stages_own_files = False

    @classmethod
    def from_settings(cls, settings: Any) -> Self:
        return cls()

    def is_job_id(self, value: str) -> bool:
        return value.startswith("/") and value.endswith("/exit")

    def submit_command(
        self, spec: SubmissionSpec, count: int, workdir: str
    ) -> SubmitCommand:
        script = posixpath.join(workdir, "start.sh")
        return SubmitCommand(
            argv=["sh", script, workdir, str(count)], files={"start.sh": _START}
        )

    def parse_submit(self, result: CommandResult, count: int) -> Sequence[JobID]:
        if result.returncode != 0:
            raise ValueError(result.stderr)
        return [JobID(line) for line in result.stdout.splitlines()]

    def status_command(self, ids: Sequence[JobID]) -> Command:
        return ["grep", "-H", "", *ids]

    def parse_status(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        rows = (line.rsplit(":", 1) for line in result.stdout.splitlines())
        return {
            JobID(job): JobStatus.DONE if code == "0" else JobStatus.FAILED
            for job, code in rows
        }

    def history_command(self, ids: Sequence[JobID]) -> Command:
        return ["true"]

    def parse_history(
        self, result: CommandResult, ids: Sequence[JobID]
    ) -> SchedulerStatusMap:
        return {}

    def kill_command(self, ids: Sequence[JobID]) -> Command:
        return ["true"]


class DirectMovingFiles(Direct):
    stages_own_files = True  # type: ignore[assignment]
