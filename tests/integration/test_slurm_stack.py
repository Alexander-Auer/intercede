"""What the `local-slurm` stack promises: the tests run as a normal user, beside Slurm's tools."""

from __future__ import annotations

import os
import subprocess
from typing import Any

import pytest

pytestmark = [pytest.mark.scheduler, pytest.mark.slurm]


@pytest.fixture
def in_container(stack: dict[str, Any]) -> None:
    if not stack.get("exec_in_container"):
        pytest.skip("this stack's tests do not run in the container")


def test_the_tests_run_as_a_normal_user(in_container: None):
    assert os.getuid() != 0


def test_that_user_can_run_a_job(in_container: None, tmp_path: Any):
    submitted = subprocess.run(  # noqa: S603  (constant input)
        ["sbatch", "--wait", f"--chdir={tmp_path}", "--wrap=true"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert submitted.returncode == 0, submitted.stderr
