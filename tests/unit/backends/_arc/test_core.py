"""The shared ARC knowledge: job descriptions, the REST interface and the state mapping.

The descriptions were checked against ARC 7.2: its own job description parser
reads every value asserted here back as written, and gives the batch system
the memory, wall time and slots expected.
"""

from __future__ import annotations

import json
from typing import Any
from xml.etree import ElementTree

import pytest

from intercede import JobStatus
from intercede._backends._arc import rest, status
from intercede._backends._arc.description import (
    ADL_NAMESPACE,
    InputFile,
    JobPlan,
    OutputFile,
    render,
)

# --------------------------------------------------------------------------
# Job descriptions
# --------------------------------------------------------------------------

NS = {"adl": ADL_NAMESPACE}


def plan(**changes: Any) -> JobPlan:
    values: dict[str, Any] = {
        "executable": "payload.sh",
        "stdout": "ic.out",
        "stderr": "ic.err",
        "arguments": ["a"],
        "environment": {"STAMP": "s0"},
        "inputs": [InputFile("payload.sh", executable=True)],
    }
    return JobPlan(**{**values, **changes})


def parse(**changes: Any) -> ElementTree.Element:
    """Render one plan and read it back, as ARC does."""
    return ElementTree.fromstring(render([plan(**changes)]))  # noqa: S314 (our own XML)


def texts(root: ElementTree.Element, path: str) -> list[str | None]:
    return [element.text for element in root.findall(path, NS)]


class TestRender:
    def test_one_plan_is_one_description(self):
        root = parse()
        assert root.tag == f"{{{ADL_NAMESPACE}}}ActivityDescription"

    def test_several_plans_go_in_one_request(self):
        text = render([plan(), plan(), plan()])
        assert not text.startswith("<?xml")  # ARC refuses a declaration
        root = ElementTree.fromstring(text)  # noqa: S314 (our own XML)
        assert root.tag == "ActivityDescriptions"
        assert [child.tag for child in root] == [
            f"{{{ADL_NAMESPACE}}}ActivityDescription"
        ] * 3

    def test_no_plan_is_refused(self):
        with pytest.raises(ValueError, match="at least one"):
            render([])

    def test_executable_and_arguments_in_order(self):
        root = parse(arguments=["b", "a", ""])
        assert texts(root, "adl:Application/adl:Executable/adl:Path") == ["payload.sh"]
        assert texts(root, "adl:Application/adl:Executable/adl:Argument") == [
            "b",
            "a",
            None,  # an empty argument is still an argument
        ]

    @pytest.mark.parametrize(
        "value",
        [
            "  edge  ",
            'it\'s "q" & $HOME <x>',
            "$(DOLLAR) `date` ;rm",
            "two\nlines",
            "ünï",
        ],
        ids=["spaces", "quotes-and-markup", "shell-and-macros", "line-feed", "unicode"],
    )
    def test_values_arrive_as_written(self, value: str):
        root = parse(arguments=[value], environment={"X": value})
        assert texts(root, "adl:Application/adl:Executable/adl:Argument") == [value]
        assert texts(root, "adl:Application/adl:Environment/adl:Value") == [value]

    @pytest.mark.parametrize(
        "value",
        ["nul\x00", "bell\x07", "carriage\rreturn"],
        ids=["nul", "control", "cr"],
    )
    def test_what_xml_cannot_carry_is_refused(self, value: str):
        with pytest.raises(ValueError, match="cannot be written"):
            render([plan(arguments=[value])])
        with pytest.raises(ValueError, match="cannot be written"):
            render([plan(environment={"X": value})])

    def test_environment_is_sorted_by_name(self):
        root = parse(environment={"B": "2", "A": "1"})
        assert texts(root, "adl:Application/adl:Environment/adl:Name") == ["A", "B"]

    @pytest.mark.parametrize("name", ["", "A=B", "WITH SPACE", "DASH-ED", "1X", "$X"])
    def test_environment_names_must_be_shell_names(self, name: str):
        with pytest.raises(ValueError, match="not a valid environment variable name"):
            render([plan(environment={name: "x"})])

    def test_stdout_and_stderr_are_kept(self):
        root = parse()
        assert texts(root, "adl:Application/adl:Output") == ["ic.out"]
        assert texts(root, "adl:Application/adl:Error") == ["ic.err"]
        assert texts(root, "adl:DataStaging/adl:OutputFile/adl:Name") == [
            "ic.out",
            "ic.err",
        ]

    def test_streams_already_listed_are_not_listed_twice(self):
        root = parse(outputs=[OutputFile("ic.out"), OutputFile("r.txt")])
        assert texts(root, "adl:DataStaging/adl:OutputFile/adl:Name") == [
            "ic.err",
            "ic.out",
            "r.txt",
        ]

    def test_inputs_uploaded_by_the_client(self):
        root = parse(
            inputs=[InputFile("payload.sh", executable=True), InputFile("secrets/t")]
        )
        assert texts(root, "adl:DataStaging/adl:ClientDataPush") == ["true"]
        files = root.findall("adl:DataStaging/adl:InputFile", NS)
        assert [texts(f, "adl:Name") for f in files] == [["payload.sh"], ["secrets/t"]]
        assert [texts(f, "adl:IsExecutable") for f in files] == [["true"], []]
        assert root.findall("adl:DataStaging/adl:InputFile/adl:Source", NS) == []

    def test_inputs_fetched_by_the_ce(self):
        root = parse(
            executable="/bin/true",
            inputs=[InputFile("in.dat", "https://example.org/in.dat")],
        )
        # Nothing to upload, so ARC need not wait for the client.
        assert texts(root, "adl:DataStaging/adl:ClientDataPush") == []
        assert texts(root, "adl:DataStaging/adl:InputFile/adl:Source/adl:URI") == [
            "https://example.org/in.dat"
        ]

    def test_outputs_kept_and_uploaded(self):
        root = parse(
            outputs=[OutputFile("r.txt"), OutputFile("up.txt", "https://s/up.txt")]
        )
        files = root.findall("adl:DataStaging/adl:OutputFile", NS)
        assert [texts(f, "adl:Name") for f in files][2:] == [["r.txt"], ["up.txt"]]
        assert texts(files[2], "adl:Target/adl:URI") == []
        assert texts(files[3], "adl:Target/adl:URI") == ["https://s/up.txt"]
        # A failed upload fails the job instead of leaving it done without its file.
        assert texts(files[3], "adl:Target/adl:Mandatory") == ["true"]

    def test_the_whole_session_directory(self):
        root = parse(outputs=[OutputFile("/")])
        assert "/" in texts(root, "adl:DataStaging/adl:OutputFile/adl:Name")

    def test_resources(self):
        root = parse(cpus=8, memory_mb=4000, wall_time_s=7200, queue="long")
        resources = "adl:Resources/adl:"
        # ADL counts bytes; ARC 7.2 gives the batch system 4000 MB again.
        assert texts(root, resources + "IndividualPhysicalMemory") == ["4194304000"]
        assert texts(root, resources + "SlotRequirement/adl:NumberOfSlots") == ["8"]
        assert texts(root, resources + "SlotRequirement/adl:SlotsPerHost") == ["8"]
        assert texts(root, resources + "WallTime") == ["7200"]
        assert texts(root, resources + "QueueName") == ["long"]

    def test_resources_left_to_the_site(self):
        root = parse()
        for name in ("IndividualPhysicalMemory", "WallTime", "QueueName"):
            assert root.findall(f"adl:Resources/adl:{name}", NS) == []
        assert texts(root, "adl:Resources/adl:SlotRequirement/adl:NumberOfSlots") == [
            "1"
        ]

    @pytest.mark.parametrize(
        "element",
        [
            "ExpirationTime",
            "WipingTime",
            "Notification",
            "PreExecutable",
            "PostExecutable",
            "FailIfExitCodeNotEqualTo",
        ],
    )
    def test_sets_no_policy_on_the_job(self, element: str):
        # When a job expires, is wiped or retried is the consumer's policy.
        assert parse().findall(f".//adl:{element}", NS) == []


# --------------------------------------------------------------------------
# The REST interface
# --------------------------------------------------------------------------


class TestPaths:
    def test_api_url(self):
        expected = "https://arc.example.org:443/arex/rest/1.1/"
        assert rest.api_url("https://arc.example.org:443/arex") == expected
        assert rest.api_url("https://arc.example.org:443/arex/") == expected

    def test_jobs_body(self):
        assert rest.jobs_body(["a1", "b2"]) == {"job": [{"id": "a1"}, {"id": "b2"}]}

    @pytest.mark.parametrize(
        ("name", "path"),
        [
            ("", "jobs/a1B2/session/"),
            ("out.txt", "jobs/a1B2/session/out.txt"),
            ("dir/a b#?.txt", "jobs/a1B2/session/dir/a%20b%23%3F.txt"),
        ],
    )
    def test_session_path(self, name: str, path: str):
        assert rest.session_path("a1B2", name) == path

    @pytest.mark.parametrize("name", ["/etc/passwd", "../other", "a/../../b"])
    def test_session_path_stays_in_the_session(self, name: str):
        with pytest.raises(ValueError, match="not inside the session directory"):
            rest.session_path("a1", name)

    @pytest.mark.parametrize("job_id", ["", "a/b", "a1?action=kill", "../x", "a 1"])
    def test_only_job_ids_reach_a_url(self, job_id: str):
        assert not rest.is_job_id(job_id)
        with pytest.raises(ValueError, match="not an ARC job ID"):
            rest.session_path(job_id)
        with pytest.raises(ValueError, match="not an ARC job ID"):
            rest.diagnose_path(job_id, "errors")

    def test_diagnose_path(self):
        assert rest.diagnose_path("a1", "errors") == "jobs/a1/diagnose/errors"
        with pytest.raises(ValueError, match="not a file ARC keeps"):
            rest.diagnose_path("a1", "../session")


def answer(*jobs: dict[str, Any]) -> str:
    return json.dumps({"job": list(jobs)})


class TestParseJobs:
    def test_one_entry_per_job(self):
        raw = answer(
            {
                "status-code": "201",
                "reason": "Created",
                "id": "a1",
                "state": "ACCEPTING",
            },
            {"status-code": "500", "reason": "Requested queue is not available"},
        )
        created, refused = rest.parse_jobs(raw)
        assert created == rest.JobAnswer(201, "Created", "a1", "ACCEPTING")
        assert created.ok
        assert refused == rest.JobAnswer(500, "Requested queue is not available")
        assert not refused.ok

    def test_a_single_entry_as_a_value(self):
        # How version 1.0 of the interface wrote one job.
        raw = json.dumps({"job": {"status-code": 200, "reason": "OK", "id": "a1"}})
        assert rest.parse_jobs(raw) == [rest.JobAnswer(200, "OK", "a1")]

    @pytest.mark.parametrize("raw", ["", "  \n", '{"job": []}', "{}"])
    def test_no_jobs(self, raw: str):
        assert rest.parse_jobs(raw) == []

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            '{"job": ["a1"]}',
            '{"job": [{"reason": "OK"}]}',
            '{"job": [{"status-code": "x", "reason": "OK"}]}',
            '{"job": [{"status-code": 200, "reason": "OK", "id": 7}]}',
        ],
        ids=["garbage", "list", "bare-id", "no-code", "bad-code", "numeric-id"],
    )
    def test_anything_else_is_refused(self, raw: str):
        with pytest.raises(ValueError):
            rest.parse_jobs(raw)


class TestParseListing:
    def test_files_and_directories(self):
        raw = json.dumps({"file": ["a.txt", "b.txt"], "dirs": ["sub"]})
        assert rest.parse_listing(raw) == rest.Listing(("a.txt", "b.txt"), ("sub",))

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", rest.Listing()),  # an empty directory
            ('{"file": "a.txt"}', rest.Listing(("a.txt",))),  # version 1.0
            ('{"dirs": "sub"}', rest.Listing((), ("sub",))),
        ],
        ids=["empty", "one-file", "one-dir"],
    )
    def test_short_answers(self, raw: str, expected: rest.Listing):
        assert rest.parse_listing(raw) == expected

    @pytest.mark.parametrize("raw", ["[]", '{"file": [1]}', "not json"])
    def test_anything_else_is_refused(self, raw: str):
        with pytest.raises(ValueError):
            rest.parse_listing(raw)


# --------------------------------------------------------------------------
# State mapping
# --------------------------------------------------------------------------


class TestJobStatus:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("ACCEPTING", JobStatus.WAITING),
            ("ACCEPTED", JobStatus.WAITING),
            ("PREPARING", JobStatus.WAITING),
            ("PREPARED", JobStatus.WAITING),
            ("SUBMITTING", JobStatus.WAITING),
            ("QUEUING", JobStatus.WAITING),
            ("RUNNING", JobStatus.RUNNING),
            ("OTHER", JobStatus.RUNNING),
            ("EXITINGLRMS", JobStatus.RUNNING),
            ("EXECUTED", JobStatus.RUNNING),
            ("FINISHING", JobStatus.RUNNING),
            ("FINISHED", JobStatus.DONE),
            ("FAILED", JobStatus.FAILED),
            ("KILLING", JobStatus.ABORTED),
            ("KILLED", JobStatus.ABORTED),
        ],
    )
    def test_states_as_in_dirac(self, state: str, expected: JobStatus):
        assert status.job_status(state) == expected

    def test_held_is_waiting(self):
        # The batch system can release it; interCEde does not kill it.
        assert status.job_status("HELD") == JobStatus.WAITING

    def test_wiped_is_unknown(self):
        # ARC removed the job's files, and with them how the job ended.
        assert status.job_status("WIPED") == JobStatus.UNKNOWN

    @pytest.mark.parametrize("state", ["Queuing", "queuing"])
    def test_any_case(self, state: str):
        assert status.job_status(state) == JobStatus.WAITING

    @pytest.mark.parametrize("state", ["", "DELETED", "None", "UNDEFINED"])
    def test_anything_else_is_unknown(self, state: str):
        assert status.job_status(state) == JobStatus.UNKNOWN
