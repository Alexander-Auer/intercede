"""The shared ARC knowledge: job descriptions.

The descriptions were checked against ARC 7.2: its own job description parser
reads every value asserted here back as written, and gives the batch system
the memory, wall time and slots expected.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

import pytest

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
