"""The shared HTCondor knowledge: submit descriptions, commands and the state mapping.

Expected values were checked against HTCondor 25 (condor_submit -dry-run and a
live schedd): what is asserted here is what HTCondor actually does with them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from intercede import JobStatus
from intercede._backends._htcondor import commands, status, submit
from intercede._backends._htcondor.submit import CopyPlan, SubmitPlan, render

# --------------------------------------------------------------------------
# Quoting
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "quoted"),
    [
        (["plain"], "\"'plain'\""),
        (["two words"], "\"'two words'\""),
        (["it's"], "\"'it''s'\""),
        (['say "hi"'], '"\'say ""hi""\'"'),
        (["cost $(HOME) $5"], "\"'cost $(DOLLAR)(HOME) $(DOLLAR)5'\""),
        ([""], "\"''\""),
        (["a", "b"], "\"'a' 'b'\""),
    ],
    ids=[
        "plain",
        "space",
        "single-quote",
        "double-quote",
        "dollar",
        "empty",
        "several",
    ],
)
def test_quote_arguments(arguments: list[str], quoted: str):
    assert submit.quote_arguments(arguments) == quoted


def test_quote_environment_sorts_and_quotes():
    quoted = submit.quote_environment({"STAMP": "a b", "X": "$(Y)", "A": "it's"})
    assert quoted == "\"A='it''s' STAMP='a b' X='$(DOLLAR)(Y)'\""


def test_quote_empty_environment():
    assert submit.quote_environment({}) == '""'


@pytest.mark.parametrize("name", ["", "A=B", "WITH SPACE", "DASH-ED", "$X"])
def test_environment_names_must_be_identifiers(name: str):
    with pytest.raises(ValueError, match="not a valid environment variable name"):
        submit.quote_environment({name: "x"})


@pytest.mark.parametrize("value", ["line\nbreak", "carriage\rreturn"])
def test_line_breaks_are_refused(value: str):
    with pytest.raises(ValueError, match="line break"):
        submit.quote_arguments([value])
    with pytest.raises(ValueError, match="line break"):
        submit.quote_environment({"X": value})


# --------------------------------------------------------------------------
# Submit descriptions
# --------------------------------------------------------------------------


def plan(**changes: Any) -> SubmitPlan:
    values: dict[str, Any] = {
        "executable": "/work/payload.sh",
        "transfer_executable": True,
        "arguments": ["a"],
        "copies": [
            CopyPlan("/work/0", {"STAMP": "s0"}, ["secrets/"]),
            CopyPlan("/work/1", {"STAMP": "s1"}),
        ],
    }
    return SubmitPlan(**{**values, **changes})


def lines(text: str) -> list[str]:
    return text.splitlines()


class TestRender:
    def test_one_process_per_copy(self):
        assert lines(render(plan()))[-1] == "queue 2"

    def test_per_copy_values_are_indexed_by_proc_id(self):
        text = render(plan())
        assert "ic_dir_0 = /work/0" in lines(text)
        assert "ic_dir_1 = /work/1" in lines(text)
        assert "ic_env_0 = \"STAMP='s0'\"" in lines(text)
        assert "ic_in_0 = secrets/" in lines(text)
        assert "ic_in_1 = " in lines(text)
        assert "initialdir = $(ic_dir_$(ProcId))" in lines(text)
        assert "environment = $(ic_env_$(ProcId))" in lines(text)

    def test_inputs_shared_and_per_copy(self):
        text = render(plan(inputs=["/work/inputs/", "https://example.org/in.dat"]))
        assert (
            "transfer_input_files = /work/inputs/, https://example.org/in.dat, $(ic_in_$(ProcId))"
            in lines(text)
        )

    def test_executable(self):
        text = render(plan(executable="/bin/sleep", transfer_executable=False))
        assert "executable = /bin/sleep" in lines(text)
        assert "transfer_executable = false" in lines(text)

    def test_no_arguments_line_without_arguments(self):
        assert not any(
            line.startswith("arguments") for line in lines(render(plan(arguments=[])))
        )

    def test_default_output_names_are_unique_per_job(self):
        text = render(plan())
        assert "output = $(ClusterId).$(ProcId).out" in lines(text)
        assert "error = $(ClusterId).$(ProcId).err" in lines(text)
        assert "log = $(ClusterId).$(ProcId).log" in lines(text)

    @pytest.mark.parametrize(
        ("outputs", "expected"),
        [
            (
                [],
                'transfer_output_files = ""',
            ),  # nothing but stdout, stderr and the log
            (["result.txt", "dir/x"], "transfer_output_files = result.txt, dir/x"),
            (None, None),  # every file the payload creates
        ],
        ids=["none", "listed", "all"],
    )
    def test_outputs(self, outputs: list[str] | None, expected: str | None):
        text = render(plan(outputs=outputs))
        found = [
            line for line in lines(text) if line.startswith("transfer_output_files")
        ]
        assert found == ([expected] if expected else [])

    def test_output_remaps_upload_to_urls(self):
        text = render(
            plan(
                outputs=["a", "b"],
                output_remaps={"b": "https://s/b", "a": "https://s/a"},
            )
        )
        assert 'transfer_output_remaps = "a = https://s/a; b = https://s/b"' in lines(
            text
        )

    def test_resources_for_a_schedd_and_for_a_ce_router(self):
        text = render(plan(cpus=8, memory_mb=4000, wall_time_s=7200, queue="long"))
        for line in [
            "request_cpus = 8",
            "+xcount = 8",
            "request_memory = 4000",
            "+maxMemory = 4000",
            "+BatchRuntime = 7200",
            '+queue = "long"',
        ]:
            assert line in lines(text)

    def test_optional_resources_are_left_out(self):
        text = render(plan())
        assert "request_cpus = 1" in lines(text)
        assert not any(
            line.startswith(("request_memory", "+maxMemory", "+BatchRuntime", "+queue"))
            for line in lines(text)
        )

    def test_queue_name_is_a_classad_string(self):
        assert '+queue = "long \\"q\\""' in lines(render(plan(queue='long "q"')))

    def test_proxy(self):
        assert "x509userproxy = /creds/proxy" in lines(
            render(plan(proxy_file="/creds/proxy"))
        )
        assert not any(
            line.startswith("x509userproxy") for line in lines(render(plan()))
        )

    def test_finished_jobs_stay_until_purged(self):
        # The one lifetime rule IC-ADR-001 §3 asks for: output can be collected
        # again until the consumer releases the job.
        assert "leave_in_queue = (JobStatus == 4)" in lines(render(plan()))

    @pytest.mark.parametrize(
        "command",
        [
            "on_exit_hold",
            "on_exit_remove",
            "periodic_hold",
            "periodic_release",
            "periodic_remove",
            "max_retries",
            "requirements",
        ],
    )
    def test_sets_no_policy_on_the_job(self, command: str):
        # Holding, releasing, removing or retrying jobs is the consumer's
        # policy, never interCEde's (IC-ADR-001 §1).
        assert not any(line.startswith(command) for line in lines(render(plan())))

    def test_macros_in_paths_are_escaped(self):
        assert "ic_dir_0 = /work/$(DOLLAR)0" in lines(
            render(plan(copies=[CopyPlan("/work/$0")]))
        )

    def test_needs_a_copy(self):
        with pytest.raises(ValueError, match="at least one copy"):
            render(plan(copies=[]))

    @pytest.mark.parametrize("field", ["inputs", "outputs"])
    def test_file_lists_cannot_hold_a_comma(self, field: str):
        with pytest.raises(ValueError, match="comma"):
            render(plan(**{field: ["a,b"]}))

    def test_paths_cannot_hold_a_line_break(self):
        with pytest.raises(ValueError, match="line break"):
            render(plan(copies=[CopyPlan("/work/a\nqueue 100")]))

    def test_ends_with_a_newline(self):
        assert render(plan()).endswith("queue 2\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

REMOTE = commands.schedd_options("ce.example.org:9619", "ce.example.org")


class TestCommands:
    def test_schedd_options(self):
        assert REMOTE == ["-pool", "ce.example.org:9619", "-name", "ce.example.org"]

    def test_submit_to_a_remote_schedd_spools(self):
        argv = commands.submit("/w/job.sub", "ce.example.org:9619", "ce.example.org")
        assert argv == [
            "condor_submit",
            "-terse",
            "-pool",
            "ce.example.org:9619",
            "-remote",
            "ce.example.org",
            "/w/job.sub",
        ]

    def test_submit_to_the_local_schedd_spools(self):
        assert commands.submit("/w/job.sub") == [
            "condor_submit",
            "-terse",
            "-spool",
            "/w/job.sub",
        ]

    @pytest.mark.parametrize(("pool", "name"), [("p:1", None), (None, "n")])
    def test_submit_needs_pool_and_name_together(
        self, pool: str | None, name: str | None
    ):
        with pytest.raises(ValueError, match="both"):
            commands.submit("/w/job.sub", pool, name)

    def test_query_and_history(self):
        ids = [status.JobID("1.0"), status.JobID("1.1")]
        tail = ["1.0", "1.1", "-attributes", "A,B", "-json"]
        assert commands.query(ids, ["A", "B"], REMOTE) == ["condor_q", *REMOTE, *tail]
        assert commands.history(ids, ["A", "B"], REMOTE) == [
            "condor_history",
            *REMOTE,
            *tail,
        ]

    def test_query_the_local_schedd(self):
        assert commands.query([status.JobID("1.0")], ["A"], ()) == [
            "condor_q",
            "1.0",
            "-attributes",
            "A",
            "-json",
        ]

    def test_remove_and_transfer(self):
        assert commands.remove([status.JobID("1.0")], REMOTE) == [
            "condor_rm",
            *REMOTE,
            "1.0",
        ]
        assert commands.transfer_data([status.JobID("1.0")], REMOTE) == [
            "condor_transfer_data",
            *REMOTE,
            "1.0",
        ]

    @pytest.mark.parametrize(
        ("count", "sizes"),
        [(0, []), (1, [1]), (100, [100]), (101, [100, 1]), (250, [100, 100, 50])],
    )
    def test_chunks(self, count: int, sizes: list[int]):
        ids = [status.JobID(f"1.{i}") for i in range(count)]
        chunked = commands.chunks(ids)
        assert [len(chunk) for chunk in chunked] == sizes
        assert [i for chunk in chunked for i in chunk] == ids


class TestParseSubmit:
    @pytest.mark.parametrize(
        ("raw", "ids"),
        [
            ("1.0 - 1.1\n", ["1.0", "1.1"]),
            ("  42.0 - 42.0  ", ["42.0"]),
            ("\n7.3 - 7.5\n\n", ["7.3", "7.4", "7.5"]),
        ],
    )
    def test_terse_output(self, raw: str, ids: list[str]):
        assert commands.parse_submit(raw) == ids

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "Submitting job(s).\n1 job(s) submitted to cluster 1.",
            "1.0 - 2.0",
            "1.3 - 1.1",
            "1.0 - 1.1\n2.0 - 2.1",
            "x",
        ],
        ids=["empty", "verbose", "two-clusters", "backwards", "two-lines", "garbage"],
    )
    def test_anything_else_is_refused(self, raw: str):
        with pytest.raises(ValueError, match="unexpected condor_submit output"):
            commands.parse_submit(raw)


def test_parse_remove():
    raw = "Job 1.0 marked for removal\nJob 1.2 marked for removal\nCouldn't find/remove all jobs matching 1.1\n"
    assert commands.parse_remove(raw) == {"1.0", "1.2"}
    assert commands.parse_remove("") == set()


# --------------------------------------------------------------------------
# State mapping
# --------------------------------------------------------------------------


def ad(**fields: Any) -> dict[str, Any]:
    return {"ClusterId": 3, "ProcId": 1, **fields}


class TestJobStatus:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (1, JobStatus.WAITING),
            (2, JobStatus.RUNNING),
            (4, JobStatus.DONE),
            (6, JobStatus.RUNNING),
            (7, JobStatus.WAITING),
        ],
        ids=["idle", "running", "completed", "transferring-output", "suspended"],
    )
    def test_plain_states(self, code: int, expected: JobStatus):
        assert status.job_status(ad(JobStatus=code)) == expected

    @pytest.mark.parametrize(
        "code",
        [0, 8, 99, None, "2"],
        ids=["zero", "eight", "large", "missing", "string"],
    )
    def test_unknown_codes(self, code: Any):
        assert status.job_status(ad(JobStatus=code)) == JobStatus.UNKNOWN

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"ExitCode": 0, "ExitBySignal": False}, JobStatus.DONE),
            ({"ExitCode": 3, "ExitBySignal": False}, JobStatus.FAILED),
            ({"ExitBySignal": True, "ExitSignal": 9}, JobStatus.FAILED),
            ({}, JobStatus.DONE),
        ],
        ids=["exit-0", "exit-3", "signal", "no-exit-info"],
    )
    def test_finished_payload_result_comes_from_its_exit(
        self, fields: dict[str, Any], expected: JobStatus
    ):
        # Read from what HTCondor records; no hold is put on the job to tell.
        assert status.job_status(ad(JobStatus=4, **fields)) == expected

    def test_spooling_is_waiting(self):
        assert (
            status.job_status(ad(JobStatus=5, HoldReasonCode=16)) == JobStatus.WAITING
        )

    @pytest.mark.parametrize(
        "fields",
        [{"HoldReasonCode": 1}, {"HoldReasonCode": 3, "HoldReasonSubCode": 55}, {}],
        ids=["by-user", "by-site-policy", "no-reason"],
    )
    def test_any_other_hold_is_aborted(self, fields: dict[str, Any]):
        assert status.job_status(ad(JobStatus=5, **fields)) == JobStatus.ABORTED

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"LastJobStatus": 4, "ExitCode": 0}, JobStatus.DONE),
            ({"LastJobStatus": 4}, JobStatus.DONE),
            ({"LastJobStatus": 4, "ExitCode": 2}, JobStatus.FAILED),
            ({"LastJobStatus": 4, "ExitBySignal": True}, JobStatus.FAILED),
            ({"LastJobStatus": 5, "ExitCode": 3}, JobStatus.ABORTED),
            ({"LastJobStatus": 2}, JobStatus.ABORTED),
            ({"LastJobStatus": 1}, JobStatus.ABORTED),
            ({}, JobStatus.ABORTED),
        ],
        ids=[
            "purged-done",
            "purged-done-no-code",
            "purged-failed",
            "purged-signal",
            "removed-while-held",
            "killed-running",
            "killed-idle",
            "bare",
        ],
    )
    def test_removed_jobs_keep_their_result(
        self, fields: dict[str, Any], expected: JobStatus
    ):
        assert status.job_status(ad(JobStatus=3, **fields)) == expected

    def test_hold_reason(self):
        assert status.hold_reason(ad(JobStatus=5, HoldReason="because")) == "because"
        assert status.hold_reason(ad(JobStatus=2, HoldReason="stale")) is None
        assert status.hold_reason(ad(JobStatus=5, HoldReason=None)) is None

    def test_job_id(self):
        assert status.job_id(ad()) == "3.1"


class TestParseAds:
    def test_condor_q_prints_nothing_for_no_match(self):
        assert status.parse_ads("") == []
        assert status.parse_ads("\n  \n") == []

    def test_condor_history_prints_an_empty_list(self):
        assert status.parse_ads("[\n]\n") == []

    def test_real_output(self):
        # Captured from HTCondor 25: payloads exiting 0, exiting 3 and killed
        # by signal 9, with no policy set on them.
        raw = json.dumps(
            [
                {"ExitBySignal": False, "ExitCode": 0, "JobStatus": 4, "ProcId": 0},
                {"ExitBySignal": False, "ExitCode": 3, "JobStatus": 4, "ProcId": 1},
                {"ExitBySignal": True, "ExitSignal": 9, "JobStatus": 4, "ProcId": 2},
            ],
            indent=2,
        )
        ads = status.parse_ads(raw)
        assert [status.job_status(a) for a in ads] == [
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.FAILED,
        ]

    def test_real_output_after_purge(self):
        # The same jobs in condor_history after condor_rm released them.
        raw = json.dumps(
            [
                {"ExitCode": 0, "JobStatus": 3, "LastJobStatus": 4, "ProcId": 0},
                {"ExitCode": 3, "JobStatus": 3, "LastJobStatus": 4, "ProcId": 1},
                {"ExitBySignal": True, "JobStatus": 3, "LastJobStatus": 4, "ProcId": 2},
            ]
        )
        assert [status.job_status(a) for a in status.parse_ads(raw)] == [
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.FAILED,
        ]

    @pytest.mark.parametrize("raw", ["{}", "[1, 2]", '"text"', "not json"])
    def test_anything_else_is_refused(self, raw: str):
        with pytest.raises(ValueError):
            status.parse_ads(raw)

    def test_every_status_query_asks_for_what_the_mapping_reads(self):
        assert {
            "ClusterId",
            "ProcId",
            "JobStatus",
            "LastJobStatus",
            "ExitCode",
            "ExitBySignal",
            "HoldReasonCode",
        } <= set(status.STATE_ATTRIBUTES)
