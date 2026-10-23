"""Shared suite: OutputRetriever, on every stack whose backend has it."""

from __future__ import annotations

from pathlib import Path

import pytest

from intercede.models import (
    CopyVars,
    FileRef,
    JobStatus,
    OutputMember,
    OutputSpec,
    SubmissionSpec,
)
from intercede.protocols import JobBackend, OutputRetriever
from tests.integration.jobs import release, script, wait_for

pytestmark = pytest.mark.contract

DONE = frozenset({JobStatus.DONE})


async def test_inputs_environment_and_outputs_round_trip(
    backend: JobBackend, output_retriever: OutputRetriever, tmp_path: Path
):
    (tmp_path / "greeting.txt").write_text("hello")
    spec = SubmissionSpec(
        executable=script(
            tmp_path,
            'cat greeting.txt; echo " $WHO"; echo to-stderr >&2; echo "$WHO" > result.txt',
        ),
        inputs=[FileRef(name="greeting.txt", source=str(tmp_path / "greeting.txt"))],
        outputs=OutputSpec(members=[OutputMember(pattern="result.txt")]),
    )
    people = ("ann", "bob")
    submission = await backend.submit(
        spec, copies=[CopyVars(environment={"WHO": who}) for who in people]
    )
    handles = [submission.handles[i] for i in range(len(people))]
    try:
        await wait_for(backend, handles, DONE)

        outputs = await output_retriever.get_output(handles, tmp_path / "out")

        for handle, who in zip(handles, people):
            outcome = outputs[handle]
            assert outcome.ok, outcome.reason
            output = outcome.value
            assert output is not None
            assert output.stdout is not None
            assert output.stdout.read_text() == f"hello {who}\n"
            assert output.stderr is not None
            assert output.stderr.read_text() == "to-stderr\n"
            assert output.files["result.txt"].read_text() == f"{who}\n"
    finally:
        await release(backend, handles)


async def test_output_can_be_collected_again(
    backend: JobBackend, output_retriever: OutputRetriever, tmp_path: Path
):
    """Retrieval is repeatable until purge (IC-ADR-001): a lost download is not lost output."""
    submission = await backend.submit(
        SubmissionSpec(executable=script(tmp_path, "echo once"))
    )
    handles = list(submission.handles.values())
    try:
        await wait_for(backend, handles, DONE)

        first = await output_retriever.get_output(handles, tmp_path / "first")
        second = await output_retriever.get_output(handles, tmp_path / "second")

        for outputs in (first, second):
            outcome = outputs[handles[0]]
            assert outcome.ok, outcome.reason
            assert outcome.value is not None
            assert outcome.value.stdout is not None
            assert outcome.value.stdout.read_text() == "once\n"
    finally:
        await release(backend, handles)
