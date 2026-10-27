"""The HTCondor-CE backend, driven against a simulated set of HTCondor tools.

``FakeCondor`` answers the way HTCondor 25 does (checked against a live schedd):
``condor_q`` prints nothing for unknown jobs, ``condor_history`` an empty list,
``condor_rm`` exits 1 for jobs it cannot find, and ``condor_transfer_data``
writes into each job's ``SUBMIT_Iwd``.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from intercede import (
    AuthenticationError,
    ConfigurationError,
    ContainerSpec,
    CopyVars,
    FileRef,
    HTCondorCEConfig,
    InterCEdeError,
    JobHandle,
    JobID,
    JobStatus,
    OutputMember,
    OutputSpec,
    ResourceUnavailableError,
    SpecificationRejectedError,
    SubmissionSpec,
    registry,
)
from intercede._backends.batch.protocols import CommandResult
from intercede._backends.monolith.htcondorce import HTCondorCEBackend
from intercede._backends.monolith.htcondorce.process import run_process
from intercede.auth import BearerToken, StaticCredentials, X509Proxy
from intercede.protocols import Diagnosable

ENDPOINT = "ce.example.org:9619"
SPEC = SubmissionSpec(executable="/bin/true")
TOKEN = "TOKEN-VALUE"  # noqa: S105  (a fake token the tests look for)


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str]
    files: dict[str, tuple[int, bytes]] = field(
        default_factory=dict
    )  # path -> (mode, content)


class FakeCondor:
    """Plays condor_submit, condor_q, condor_history, condor_rm and condor_transfer_data."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.queue: dict[str, dict[str, Any]] = {}
        self.history: dict[str, dict[str, Any]] = {}
        self.outputs: dict[str, dict[str, bytes]] = {}  # files a job "created"
        self.descriptions: list[str] = []
        self.next_cluster = 1
        self.fail: dict[str, CommandResult] = {}  # tool -> forced result

    async def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> CommandResult:
        call = Call(list(argv), dict(env))
        self.calls.append(call)
        self._snapshot(call)
        tool, args = argv[0], [a for a in argv[1:] if a != "-debug"]
        if tool in self.fail:
            return self.fail[tool]
        ids = [a for a in args if re.fullmatch(r"\d+\.\d+", a)]
        if tool == "condor_submit":
            return self._submit(Path(args[-1]))
        if tool == "condor_q":
            return self._ads(self.queue, ids, args, empty="")
        if tool == "condor_history":
            return self._ads(self.history, ids, args, empty="[\n]\n")
        if tool == "condor_rm":
            return self._remove(ids)
        if tool == "condor_transfer_data":
            return self._transfer(ids)
        raise AssertionError(f"unexpected tool {tool}")

    # -- the tools -----------------------------------------------------------

    def _submit(self, description: Path) -> CommandResult:
        text = description.read_text()
        self.descriptions.append(text)
        count = int(re.search(r"^queue (\d+)$", text, re.M).group(1))  # type: ignore[union-attr]
        cluster = self.next_cluster
        self.next_cluster += 1
        for proc in range(count):
            iwd = re.search(rf"^ic_dir_{proc} = (.*)$", text, re.M).group(1)  # type: ignore[union-attr]
            out = re.search(r"^output = (.*)$", text, re.M).group(1)  # type: ignore[union-attr]
            err = re.search(r"^error = (.*)$", text, re.M).group(1)  # type: ignore[union-attr]
            self.queue[f"{cluster}.{proc}"] = {
                "ClusterId": cluster,
                "ProcId": proc,
                "JobStatus": 1,
                "SUBMIT_Iwd": iwd,
                "Out": _expand(out, cluster, proc),
                "Err": _expand(err, cluster, proc),
                "UserLog": f"{iwd}/{cluster}.{proc}.log",
            }
        return ok(f"{cluster}.0 - {cluster}.{count - 1}\n")

    @staticmethod
    def _ads(
        store: Mapping[str, dict[str, Any]], ids: list[str], args: list[str], empty: str
    ) -> CommandResult:
        wanted = args[args.index("-attributes") + 1].split(",")
        found = [
            {k: v for k, v in store[i].items() if k in wanted}
            for i in ids
            if i in store
        ]
        return ok(json.dumps(found) if found else empty)

    def _remove(self, ids: list[str]) -> CommandResult:
        lines, missing = [], False
        for i in ids:
            if i in self.queue:
                ad = self.queue.pop(i)
                self.history[i] = {
                    **ad,
                    "LastJobStatus": ad["JobStatus"],
                    "JobStatus": 3,
                }
                lines.append(f"Job {i} marked for removal")
            else:
                missing = True
        if missing:
            return CommandResult(
                returncode=1,
                stdout="\n".join(lines),
                stderr="Couldn't find/remove all jobs\n",
            )
        return ok("\n".join(lines))

    def _transfer(self, ids: list[str]) -> CommandResult:
        for i in ids:
            ad = self.queue[i]
            iwd = Path(ad["SUBMIT_Iwd"])
            iwd.mkdir(parents=True, exist_ok=True)
            files = {
                name: content
                for name, content in [
                    (ad.get("Out"), b"stdout of " + i.encode()),
                    (ad.get("Err"), b""),
                    (Path(ad["UserLog"]).name if "UserLog" in ad else None, b"log"),
                ]
                if name
            }
            for name, content in {**files, **self.outputs.get(i, {})}.items():
                (iwd / name).parent.mkdir(parents=True, exist_ok=True)
                (iwd / name).write_bytes(content)
        return ok("Fetching data files...\n")

    # -- helpers for the tests --------------------------------------------------

    def _snapshot(self, call: Call) -> None:
        """Record staged and credential files as they exist during the call."""
        for key in ("_CONDOR_SCITOKENS_FILE",):
            if key in call.env:
                self._record(call, Path(call.env[key]))
        if call.argv[0] == "condor_submit":
            submission = Path(call.argv[-1]).parent
            for path in submission.rglob("*"):
                if path.is_file():
                    self._record(call, path)
            proxy = re.search(
                r"^x509userproxy = (.*)$", Path(call.argv[-1]).read_text(), re.M
            )
            if proxy:
                self._record(call, Path(proxy.group(1)))

    @staticmethod
    def _record(call: Call, path: Path) -> None:
        if path.is_file():
            call.files[str(path)] = (
                stat.S_IMODE(path.stat().st_mode),
                path.read_bytes(),
            )

    def tools(self) -> list[str]:
        return [call.argv[0] for call in self.calls]

    def finish(self, job: JobID | str, code: int = 4, **fields: Any) -> None:
        self.queue[job] = {**self.queue[job], "JobStatus": code, **fields}


def _expand(name: str, cluster: int, proc: int) -> str:
    return name.replace("$(ClusterId)", str(cluster)).replace("$(ProcId)", str(proc))


def ok(stdout: str) -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


def settings(**fields: Any) -> HTCondorCEConfig:
    return HTCondorCEConfig(**{"kind": "htcondor-ce", "endpoint": ENDPOINT, **fields})


@pytest.fixture
def condor() -> FakeCondor:
    return FakeCondor()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path / "work"


def make(
    condor: FakeCondor,
    workdir: Path,
    credentials: list[Any] | None = None,
    **fields: Any,
) -> HTCondorCEBackend:
    provider = StaticCredentials(
        credentials if credentials is not None else [BearerToken(value=TOKEN)]
    )
    return HTCondorCEBackend(
        settings(**fields), provider, runner=condor, workdir=workdir
    )


@pytest.fixture
async def ce(condor: FakeCondor, workdir: Path):
    async with make(condor, workdir) as backend:
        yield backend


# --------------------------------------------------------------------------
# Shape and construction
# --------------------------------------------------------------------------


class TestShape:
    def test_no_live_diagnostics(self, condor: FakeCondor, workdir: Path):
        assert not isinstance(make(condor, workdir), Diagnosable)

    def test_construction_does_no_io(self, condor: FakeCondor, workdir: Path):
        make(condor, workdir)
        assert not workdir.exists()
        assert condor.calls == []

    def test_built_by_the_registry(self):
        built = registry.get_backend(
            {"kind": "htcondor-ce", "endpoint": ENDPOINT}, StaticCredentials()
        )
        assert isinstance(built, HTCondorCEBackend)
        assert built.resource == ENDPOINT

    @pytest.mark.parametrize(
        ("endpoint", "pool"),
        [
            ("ce.example.org:9619", "ce.example.org:9619"),
            ("ce.example.org", "ce.example.org:9619"),
            ("ce:1234", "ce:1234"),
        ],
    )
    async def test_endpoint_becomes_pool_and_schedd_name(
        self, condor: FakeCondor, workdir: Path, endpoint: str, pool: str
    ):
        backend = HTCondorCEBackend(
            settings(endpoint=endpoint),
            StaticCredentials([BearerToken(value=TOKEN)]),
            runner=condor,
            workdir=workdir,
        )
        async with backend:
            await backend.get_status(
                [JobHandle(backend="htcondor-ce", resource=endpoint, id=JobID("1.0"))]
            )
        argv = condor.calls[0].argv
        assert argv[argv.index("-pool") + 1] == pool
        assert argv[argv.index("-name") + 1] == pool.split(":")[0]

    async def test_schedd_overrides_the_name_not_the_pool(
        self, condor: FakeCondor, workdir: Path
    ):
        async with make(condor, workdir, schedd="ce01@ce.example.org") as backend:
            await backend.get_status(
                [JobHandle(backend="htcondor-ce", resource=ENDPOINT, id=JobID("1.0"))]
            )
        argv = condor.calls[0].argv
        assert argv[argv.index("-pool") + 1] == ENDPOINT
        assert argv[argv.index("-name") + 1] == "ce01@ce.example.org"

    async def test_closed_backend_refuses(self, condor: FakeCondor, workdir: Path):
        backend = make(condor, workdir)
        async with backend:
            pass
        await backend.__aexit__(None, None, None)  # closing twice is harmless
        with pytest.raises(InterCEdeError, match="closed"):
            await backend.submit(SPEC)


# --------------------------------------------------------------------------
# How every tool runs
# --------------------------------------------------------------------------


class TestEnvironment:
    async def test_every_tool_runs_with_debug_for_error_classification(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        await ce.submit(SPEC)
        assert all(call.argv[1] == "-debug" for call in condor.calls)

    async def test_authentication_is_required_and_token_based(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        await ce.submit(SPEC)
        env = condor.calls[0].env
        assert env["_CONDOR_SEC_CLIENT_AUTHENTICATION"] == "REQUIRED"
        assert env["_CONDOR_SEC_CLIENT_AUTHENTICATION_METHODS"] == "SCITOKENS,IDTOKENS"
        assert env["_CONDOR_TOOL_DEBUG"] == "D_SECURITY"

    async def test_token_is_a_private_file_only_during_the_call(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        await ce.submit(SPEC)
        call = condor.calls[0]
        token = call.env["_CONDOR_SCITOKENS_FILE"]
        assert call.files[token] == (0o600, TOKEN.encode())
        assert not Path(token).exists()
        assert not Path(call.env["_CONDOR_SEC_TOKEN_DIRECTORY"]).exists()
        assert not Path(call.env["_CONDOR_LOG"]).exists()

    async def test_no_config_file_unless_the_operator_set_one(
        self, ce: HTCondorCEBackend, condor: FakeCondor, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("CONDOR_CONFIG", raising=False)
        await ce.get_status(
            [JobHandle(backend="htcondor-ce", resource=ENDPOINT, id=JobID("1.0"))]
        )
        assert condor.calls[-1].env["CONDOR_CONFIG"] == "ONLY_ENV"
        monkeypatch.setenv("CONDOR_CONFIG", "/etc/condor/site.conf")
        await ce.get_status(
            [JobHandle(backend="htcondor-ce", resource=ENDPOINT, id=JobID("1.0"))]
        )
        assert condor.calls[-1].env["CONDOR_CONFIG"] == "/etc/condor/site.conf"

    async def test_ca_directory_from_x509_cert_dir(
        self, ce: HTCondorCEBackend, condor: FakeCondor, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("X509_CERT_DIR", "/etc/grid-security/certificates")
        await ce.submit(SPEC)
        assert (
            condor.calls[0].env["_CONDOR_AUTH_SSL_CLIENT_CADIR"]
            == "/etc/grid-security/certificates"
        )

    async def test_scopes_follow_the_operation(self, condor: FakeCondor, workdir: Path):
        asked: list[frozenset[str]] = []

        class Recording(StaticCredentials):
            async def get(self, requirements: Any) -> Any:
                asked.append(requirements.scopes)
                return await super().get(requirements)

        handle = JobHandle(backend="htcondor-ce", resource=ENDPOINT, id=JobID("1.0"))
        backend = HTCondorCEBackend(
            settings(),
            Recording([BearerToken(value=TOKEN)]),
            runner=condor,
            workdir=workdir,
        )
        async with backend:
            await backend.submit(SPEC)
            await backend.get_status([handle])
            await backend.kill([handle])
        assert asked[:2] == [frozenset({"compute.create"}), frozenset({"compute.read"})]


class TestErrors:
    @pytest.mark.parametrize(
        "stderr",
        [
            "10/21/26 13:52:19 AUTHENTICATE:1003:Failed to authenticate with any method\n"
            "Error: Couldn't contact the condor_collector",
            "PERMISSION DENIED to submituser",
            # condor_submit with a rejected token (HTCondor 25): no AUTHENTICATE
            # summary, and an error that looks like an unreachable schedd.
            "10/26/26 11:37:15 AUTHENTICATE: method 2048 (IDTOKENS) failed.\n"
            "10/26/26 11:37:15 SECMAN: required authentication with collector "
            "127.0.0.1:9619 failed, so aborting command QUERY_SCHEDD_ADS.\n"
            "ERROR: Can't find address of schedd htcondor-ce@backend",
        ],
        ids=["token-rejected", "permission-denied", "submit-token-rejected"],
    )
    async def test_authentication_failure(
        self, ce: HTCondorCEBackend, condor: FakeCondor, stderr: str
    ):
        condor.fail["condor_submit"] = CommandResult(
            returncode=1, stdout="", stderr=stderr
        )
        with pytest.raises(AuthenticationError):
            await ce.submit(SPEC)

    @pytest.mark.parametrize(
        "stderr",
        [
            "10/21/26 13:52:19 connect errno = 111\nError: Couldn't contact the condor_collector on ce:9619.",
            "Can't find address for schedd ce.example.org",
        ],
        ids=["refused", "no-address"],
    )
    async def test_unreachable(
        self, ce: HTCondorCEBackend, condor: FakeCondor, stderr: str
    ):
        condor.fail["condor_q"] = CommandResult(returncode=1, stdout="", stderr=stderr)
        with pytest.raises(ResourceUnavailableError, match="could not be reached"):
            await ce.get_status(
                [JobHandle(backend="htcondor-ce", resource=ENDPOINT, id=JobID("1.0"))]
            )

    async def test_other_failures_name_the_tool_without_debug_lines(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        condor.fail["condor_submit"] = CommandResult(
            returncode=1,
            stdout="",
            stderr="10/21/26 13:52:19 SECMAN: noise\nERROR: on Line 3 of submit file\n",
        )
        with pytest.raises(
            ResourceUnavailableError,
            match="condor_submit failed: ERROR: on Line 3 of submit file$",
        ):
            await ce.submit(SPEC)

    @pytest.mark.parametrize("raw", ["Submitting job(s).", "1.0 - 1.0\n2.0 - 2.0"])
    async def test_unreadable_submit_output(
        self, ce: HTCondorCEBackend, condor: FakeCondor, raw: str
    ):
        condor.fail["condor_submit"] = ok(raw)
        with pytest.raises(
            ResourceUnavailableError, match="unexpected condor_submit output"
        ):
            await ce.submit(SPEC)

    async def test_job_count_mismatch(self, ce: HTCondorCEBackend, condor: FakeCondor):
        condor.fail["condor_submit"] = ok("5.0 - 5.0")
        with pytest.raises(
            ResourceUnavailableError, match="created 1 jobs, expected 2"
        ):
            await ce.submit(SPEC, 2)

    async def test_unreadable_query_output(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        condor.fail["condor_q"] = ok("not json")
        with pytest.raises(ResourceUnavailableError, match="unreadable output"):
            await ce.get_status(
                [JobHandle(backend="htcondor-ce", resource=ENDPOINT, id=JobID("1.0"))]
            )

    async def test_no_token_from_the_provider(self, condor: FakeCondor, workdir: Path):
        async with make(condor, workdir, credentials=[]) as backend:
            with pytest.raises(AuthenticationError, match="BearerToken"):
                await backend.submit(SPEC)
        assert condor.calls == []


class TestRunProcess:
    async def test_runs_a_command_with_only_the_given_environment(self):
        result = await run_process(
            ["/bin/sh", "-c", "echo $ONLY; echo err >&2; exit 3"],
            {"ONLY": "this", "PATH": os.environ["PATH"]},
            10,
        )
        assert (result.returncode, result.stdout, result.stderr) == (
            3,
            "this\n",
            "err\n",
        )

    async def test_missing_tool_is_a_configuration_error(self):
        with pytest.raises(ConfigurationError, match="HTCondor client tools"):
            await run_process(["condor_definitely_not_installed"], dict(os.environ), 10)

    async def test_timeout_kills_the_tool(self):
        with pytest.raises(ResourceUnavailableError, match="did not finish"):
            await run_process(["/bin/sleep", "10"], dict(os.environ), 0.2)


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------


class TestSubmit:
    async def test_a_secret_named_like_an_input_is_refused_before_any_call(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        source = tmp_path / "pilot.cfg"
        source.write_text("data")
        spec = SPEC.model_copy(
            update={"inputs": [FileRef(name="pilot.cfg", source=str(source))]}
        )
        with pytest.raises(SpecificationRejectedError, match="pilot.cfg"):
            await ce.submit(spec, [CopyVars(secrets={"pilot.cfg": b"s"})])
        assert condor.calls == []

    async def test_zero_copies_submit_nothing(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        assert (await ce.submit(SPEC, 0)).handles == {}
        assert condor.calls == []

    async def test_local_inputs_keep_their_directories(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        source = tmp_path / "anything.dat"
        source.write_text("data")
        spec = SPEC.model_copy(
            update={"inputs": [FileRef(name="sub/dir/in.txt", source=str(source))]}
        )
        await ce.submit(spec)
        staged = [
            p for p in condor.calls[0].files if p.endswith("inputs/sub/dir/in.txt")
        ]
        assert len(staged) == 1 and condor.calls[0].files[staged[0]][1] == b"data"

    async def test_secrets_are_private_and_gone_afterwards(
        self, ce: HTCondorCEBackend, condor: FakeCondor, workdir: Path
    ):
        copies = [
            CopyVars(secrets={"pilot.secret": b"s0"}),
            CopyVars(secrets={"pilot.secret": b"s1"}),
        ]
        await ce.submit(SPEC, copies)
        secrets = {
            path: entry
            for path, entry in condor.calls[0].files.items()
            if path.endswith("pilot.secret")
        }
        assert sorted(entry for entry in secrets.values()) == [
            (0o600, b"s0"),
            (0o600, b"s1"),
        ]
        assert not [p for p in workdir.rglob("*") if p.is_file()]

    async def test_secret_never_in_the_description(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        await ce.submit(SPEC, [CopyVars(secrets={"pilot.secret": b"TOPSECRET"})])
        assert "TOPSECRET" not in condor.descriptions[0]

    async def test_different_variable_names_get_separate_clusters(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        # HTCondor merges each process's environment onto the previous one, so
        # a variable of one copy would leak into the next in the same cluster.
        copies = [
            CopyVars(environment={"STAMP": "a"}),
            CopyVars(environment={"STAMP": "b", "EXTRA": "x"}),
            CopyVars(environment={"STAMP": "c"}),
        ]
        sub = await ce.submit(SPEC, copies)
        assert condor.tools().count("condor_submit") == 2
        clusters = {i: h.id.split(".")[0] for i, h in sub.handles.items()}
        assert clusters[0] == clusters[2] != clusters[1]
        assert "EXTRA" not in condor.descriptions[0]

    async def test_a_later_cluster_failing_keeps_the_submitted_copies(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        copies = [CopyVars(environment={"A": "1"}), CopyVars(environment={"B": "2"})]
        submitted = 0
        real = condor.__call__

        async def second_submit_fails(
            argv: Sequence[str], env: Mapping[str, str], timeout: float
        ) -> CommandResult:
            nonlocal submitted
            if argv[0] == "condor_submit":
                submitted += 1
                if submitted == 2:
                    return CommandResult(
                        returncode=1, stdout="", stderr="ERROR: schedd full"
                    )
            return await real(argv, env, timeout)

        backend = HTCondorCEBackend(
            settings(),
            StaticCredentials([BearerToken(value=TOKEN)]),
            runner=second_submit_fails,
            workdir=ce._workdir,
        )
        async with backend:
            sub = await backend.submit(SPEC, copies)
        assert sorted(sub.handles) == [0]
        assert sub.failures == {1: "condor_submit failed: ERROR: schedd full"}

    async def test_nothing_submitted_raises(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        condor.fail["condor_submit"] = CommandResult(
            returncode=1, stdout="", stderr="ERROR: schedd full"
        )
        with pytest.raises(ResourceUnavailableError, match="schedd full"):
            await ce.submit(SPEC, 2)

    async def test_url_inputs_are_left_to_the_resource(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        spec = SPEC.model_copy(
            update={
                "inputs": [
                    FileRef(name="in.dat", source="https://example.org/data/in.dat?x=1")
                ]
            }
        )
        await ce.submit(spec)
        assert "https://example.org/data/in.dat?x=1" in condor.descriptions[0]

    async def test_url_input_under_another_name_is_refused(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        spec = SPEC.model_copy(
            update={
                "inputs": [
                    FileRef(name="renamed.dat", source="https://example.org/in.dat")
                ]
            }
        )
        with pytest.raises(SpecificationRejectedError, match="another name"):
            await ce.submit(spec)
        assert "condor_submit" not in condor.tools()

    async def test_missing_local_input_is_refused(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        spec = SPEC.model_copy(
            update={"inputs": [FileRef(name="in", source=str(tmp_path / "missing"))]}
        )
        with pytest.raises(SpecificationRejectedError, match="cannot read"):
            await ce.submit(spec)
        assert "condor_submit" not in condor.tools()

    async def test_command_on_the_worker_is_not_shipped(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        # Not a file on this host, so it is a command on the worker node.
        await ce.submit(
            SPEC.model_copy(update={"executable": "/opt/site/bin/run-payload"})
        )
        assert "transfer_executable = false" in condor.descriptions[0]

    async def test_resources_and_queue(self, condor: FakeCondor, workdir: Path):
        spec = SPEC.model_copy(
            update={
                "resources": SPEC.resources.model_copy(
                    update={"cpus": 4, "memory_mb": 8000, "wall_time_s": 3600}
                )
            }
        )
        async with make(condor, workdir, queue="default") as backend:
            await backend.submit(spec)
        text = condor.descriptions[0]
        assert all(
            line in text
            for line in [
                "+xcount = 4",
                "+maxMemory = 8000",
                "+BatchRuntime = 3600",
                '+queue = "default"',
            ]
        )

    async def test_spec_queue_wins_over_configured_queue(
        self, condor: FakeCondor, workdir: Path
    ):
        spec = SPEC.model_copy(
            update={"resources": SPEC.resources.model_copy(update={"queue": "short"})}
        )
        async with make(condor, workdir, queue="default") as backend:
            await backend.submit(spec)
        assert '+queue = "short"' in condor.descriptions[0]


class TestSubmitOutputs:
    async def test_kept_and_uploaded_members(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        outputs = OutputSpec(
            members=[
                OutputMember(pattern="kept.txt"),
                OutputMember(pattern="sent.root", destination="https://se/sent.root"),
            ]
        )
        await ce.submit(SPEC.model_copy(update={"outputs": outputs}))
        text = condor.descriptions[0]
        assert "transfer_output_files = kept.txt, sent.root" in text
        assert 'transfer_output_remaps = "sent.root = https://se/sent.root"' in text

    async def test_include_unlisted_brings_back_everything(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        outputs = OutputSpec(
            members=[OutputMember(pattern="*.root")], include_unlisted=True
        )
        await ce.submit(SPEC.model_copy(update={"outputs": outputs}))
        assert "transfer_output_files" not in condor.descriptions[0]

    @pytest.mark.parametrize(
        "member",
        [
            OutputMember(pattern="*.root"),
            OutputMember(pattern="out?.txt"),
            OutputMember(pattern="[ab].txt"),
        ],
    )
    async def test_patterns_are_refused_without_include_unlisted(
        self, ce: HTCondorCEBackend, member: OutputMember
    ):
        with pytest.raises(SpecificationRejectedError, match="by name, not by pattern"):
            await ce.submit(
                SPEC.model_copy(update={"outputs": OutputSpec(members=[member])})
            )

    async def test_a_pattern_cannot_be_uploaded(self, ce: HTCondorCEBackend):
        member = OutputMember(pattern="*.root", destination="https://se/")
        with pytest.raises(SpecificationRejectedError, match="by name"):
            await ce.submit(
                SPEC.model_copy(
                    update={
                        "outputs": OutputSpec(members=[member], include_unlisted=True)
                    }
                )
            )


class TestContainers:
    async def test_refused_without_a_launcher(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        with pytest.raises(SpecificationRejectedError, match="no container_launcher"):
            await ce.submit(
                SPEC.model_copy(
                    update={"container": ContainerSpec(image="docker://alpine")}
                )
            )
        assert condor.calls == []

    async def test_wrapped_by_the_configured_launcher(
        self, condor: FakeCondor, workdir: Path
    ):
        spec = SubmissionSpec(
            executable="/usr/bin/run",
            arguments=["x"],
            container=ContainerSpec(image="/cvmfs/img.sif"),
        )
        async with make(
            condor, workdir, container_launcher=["apptainer", "exec", "{image}"]
        ) as backend:
            await backend.submit(spec)
        text = condor.descriptions[0]
        assert "executable = apptainer" in text
        assert "transfer_executable = false" in text
        assert "arguments = \"'exec' '/cvmfs/img.sif' '/usr/bin/run' 'x'\"" in text

    async def test_a_local_file_cannot_run_inside_an_image(
        self, condor: FakeCondor, workdir: Path, tmp_path: Path
    ):
        script = tmp_path / "s.sh"
        script.write_text("")
        spec = SubmissionSpec(
            executable=str(script), container=ContainerSpec(image="docker://a")
        )
        async with make(
            condor, workdir, container_launcher=["apptainer", "exec", "{image}"]
        ) as backend:
            with pytest.raises(SpecificationRejectedError, match="inside the image"):
                await backend.submit(spec)


class TestProxy:
    async def test_proxy_rides_in_the_job_without_delegation(
        self, condor: FakeCondor, workdir: Path
    ):
        credentials = [BearerToken(value=TOKEN), X509Proxy(pem=b"PROXY-PEM")]
        async with make(
            condor, workdir, credentials=credentials, auth=["token", "proxy"]
        ) as backend:
            await backend.submit(SPEC)
        call = condor.calls[0]
        proxy = re.search(r"^x509userproxy = (.*)$", condor.descriptions[0], re.M)
        assert proxy and call.files[proxy.group(1)] == (0o600, b"PROXY-PEM")
        assert not Path(proxy.group(1)).exists()
        assert call.env["_CONDOR_DELEGATE_JOB_GSI_CREDENTIALS"] == "false"

    async def test_token_only_has_no_proxy(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        await ce.submit(SPEC)
        assert "x509userproxy" not in condor.descriptions[0]
        assert "_CONDOR_DELEGATE_JOB_GSI_CREDENTIALS" not in condor.calls[0].env


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def handle(job: str, **changes: str) -> JobHandle:
    return JobHandle(
        backend="htcondor-ce", resource=ENDPOINT, id=JobID(job)
    ).model_copy(update=changes)


class TestStatus:
    async def test_queue_then_history_for_the_rest(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        sub = await ce.submit(SPEC, 2)
        condor.finish("1.0")
        await ce.purge([sub.handles[1]])
        condor.calls.clear()
        states = await ce.get_status(list(sub.handles.values()))
        assert states == {
            sub.handles[0]: JobStatus.DONE,
            sub.handles[1]: JobStatus.ABORTED,
        }
        assert condor.tools() == ["condor_q", "condor_history"]
        history_ids = [a for a in condor.calls[1].argv if re.fullmatch(r"\d+\.\d+", a)]
        assert history_ids == ["1.1"]  # only what the queue did not know

    @pytest.mark.parametrize(
        "foreign",
        [
            {"resource": "other.example.org:9619"},
            {"backend": "arc"},
            {"id": "-constraint true"},
            {"id": "1"},
            {"id": "1.0 1.1"},
        ],
        ids=["other-ce", "other-backend", "option-injection", "no-proc", "two-ids"],
    )
    async def test_foreign_and_malformed_handles_never_reach_a_command(
        self, ce: HTCondorCEBackend, condor: FakeCondor, foreign: dict[str, str]
    ):
        bad = handle("1.0", **foreign)
        assert await ce.get_status([bad]) == {bad: JobStatus.UNKNOWN}
        assert condor.calls == []

    async def test_large_polls_are_chunked(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        await ce.get_status([handle(f"1.{i}") for i in range(250)])
        assert condor.tools() == ["condor_q"] * 3 + ["condor_history"] * 3


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


class TestOutput:
    async def test_collects_into_dest_with_a_manifest(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id)
        condor.outputs[h.id] = {"result.txt": b"42", "sub/dir/data.bin": b"\x00"}
        outcome = (await ce.get_output([h], tmp_path / "dest"))[h]
        assert outcome.ok and outcome.value is not None
        target = tmp_path / "dest" / "ce.example.org" / h.id
        assert outcome.value.stdout == target / "1.0.out"
        assert outcome.value.stdout.read_bytes() == b"stdout of 1.0"
        assert outcome.value.stderr == target / "1.0.err"
        assert outcome.value.log == target / "1.0.log"
        assert outcome.value.files == {
            "result.txt": target / "result.txt",
            "sub/dir/data.bin": target / "sub/dir/data.bin",
        }

    async def test_custom_stdout_name_is_recognised(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (
            await ce.submit(SPEC.model_copy(update={"stdout": "logs/pilot.out"}))
        ).handles[0]
        condor.finish(h.id)
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert outcome.value is not None and outcome.value.stdout is not None
        assert outcome.value.stdout.name == "pilot.out"
        assert "logs/pilot.out" not in outcome.value.files

    async def test_repeatable_and_replacing(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id)
        condor.outputs[h.id] = {"old.txt": b"1"}
        await ce.get_output([h], tmp_path)
        condor.outputs[h.id] = {"new.txt": b"2"}
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert outcome.value is not None and set(outcome.value.files) == {"new.txt"}
        assert not (tmp_path / "ce.example.org" / h.id / "old.txt").exists()

    async def test_collection_area_is_cleaned(
        self, ce: HTCondorCEBackend, condor: FakeCondor, workdir: Path, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id)
        await ce.get_output([h], tmp_path / "dest")
        assert not [p for p in workdir.rglob("*") if p.is_file()]

    @pytest.mark.parametrize("code", [1, 2], ids=["waiting", "running"])
    async def test_active_jobs(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path, code: int
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id, code=code)
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert not outcome.ok and outcome.reason == "job has not finished"
        assert "condor_transfer_data" not in condor.tools()

    async def test_failed_jobs_can_be_collected(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id, ExitCode=3)  # completed, payload exited non-zero
        assert (await ce.get_output([h], tmp_path))[h].ok

    async def test_job_that_left_the_queue(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        await ce.purge([h])
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert (
            not outcome.ok
            and outcome.reason == "the job has left the queue; its output is gone"
        )

    @pytest.mark.parametrize(
        "iwd", ["/etc", "relative/dir", "{workdir}", "{workdir}/../escape"]
    )
    async def test_the_ce_cannot_choose_where_we_write(
        self,
        ce: HTCondorCEBackend,
        condor: FakeCondor,
        workdir: Path,
        tmp_path: Path,
        iwd: str,
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id, SUBMIT_Iwd=iwd.format(workdir=workdir))
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert (
            not outcome.ok
            and outcome.reason
            == "the job's output directory is outside our working area"
        )
        assert "condor_transfer_data" not in condor.tools()

    async def test_transfer_failure_is_per_job(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id)
        condor.fail["condor_transfer_data"] = CommandResult(
            returncode=1, stdout="", stderr="ERROR: spool gone"
        )
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert (
            not outcome.ok
            and outcome.reason == "condor_transfer_data failed: ERROR: spool gone"
        )

    async def test_too_many_files(
        self, condor: FakeCondor, workdir: Path, tmp_path: Path
    ):
        async with make(condor, workdir, max_output_files=3) as backend:
            h = (await backend.submit(SPEC)).handles[0]
            condor.finish(h.id)
            condor.outputs[h.id] = {"a": b"", "b": b""}  # plus stdout, stderr and log
            outcome = (await backend.get_output([h], tmp_path))[h]
        assert not outcome.ok and outcome.reason == "more than 3 output files"

    async def test_too_many_bytes(
        self, condor: FakeCondor, workdir: Path, tmp_path: Path
    ):
        async with make(condor, workdir, max_output_bytes=10) as backend:
            h = (await backend.submit(SPEC)).handles[0]
            condor.finish(h.id)
            condor.outputs[h.id] = {"big": b"x" * 100}
            outcome = (await backend.get_output([h], tmp_path))[h]
        assert not outcome.ok and outcome.reason == "output larger than 10 bytes"

    async def test_links_are_refused(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id)
        real = condor._transfer

        def with_link(ids: list[str]) -> CommandResult:
            result = real(ids)
            (Path(condor.queue[h.id]["SUBMIT_Iwd"]) / "link").symlink_to("/etc/passwd")
            return result

        condor._transfer = with_link  # type: ignore[method-assign]
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert not outcome.ok and "link" in (outcome.reason or "")

    async def test_transfer_uses_the_configured_timeout(
        self, condor: FakeCondor, workdir: Path, tmp_path: Path
    ):
        timeouts: dict[str, float] = {}

        async def recording(
            argv: Sequence[str], env: Mapping[str, str], timeout: float
        ) -> CommandResult:
            timeouts[argv[0]] = timeout
            return await condor(argv, env, timeout)

        backend = HTCondorCEBackend(
            settings(transfer_timeout_s=42),
            StaticCredentials([BearerToken(value=TOKEN)]),
            runner=recording,
            workdir=workdir,
        )
        async with backend:
            h = (await backend.submit(SPEC)).handles[0]
            condor.finish(h.id)
            await backend.get_output([h], tmp_path)
        assert timeouts["condor_transfer_data"] == 42
        assert timeouts["condor_submit"] == 120


# --------------------------------------------------------------------------
# Kill and purge
# --------------------------------------------------------------------------


class TestKill:
    async def test_a_job_the_ce_no_longer_knows(self, ce: HTCondorCEBackend):
        gone = handle("9.0")
        assert (await ce.kill([gone]))[gone].reason == "unknown job"
        assert (await ce.purge([gone]))[gone].reason == "unknown job"

    @pytest.mark.parametrize(
        "fields",
        [{"code": 4, "ExitCode": 0}, {"code": 4, "ExitCode": 3}],
        ids=["done", "failed"],
    )
    async def test_leaves_finished_jobs_and_their_output(
        self, ce: HTCondorCEBackend, condor: FakeCondor, fields: dict[str, Any]
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id, **fields)
        condor.calls.clear()
        outcome = (await ce.kill([h]))[h]
        assert outcome.ok and outcome.reason == "already finished"
        assert "condor_rm" not in condor.tools()

    async def test_condor_rm_refusing_one_job(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        sub = await ce.submit(SPEC, 2)
        real = condor._remove

        def refuse_second(ids: list[str]) -> CommandResult:
            result = real(ids[:1])
            return CommandResult(
                returncode=1,
                stdout=result.stdout,
                stderr="Couldn't find/remove all jobs\n",
            )

        condor._remove = refuse_second  # type: ignore[method-assign]
        outcomes = await ce.kill(list(sub.handles.values()))
        assert outcomes[sub.handles[0]].ok
        assert not outcomes[sub.handles[1]].ok
        assert (
            outcomes[sub.handles[1]].reason
            == "condor_rm failed: Couldn't find/remove all jobs"
        )


class TestPurge:
    async def test_a_job_removed_but_still_queued_is_already_released(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        # condor_rm returns before the schedd drops the job from the queue.
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id, code=3, LastJobStatus=4)
        condor.calls.clear()
        output = (await ce.get_output([h], tmp_path))[h]
        assert (
            not output.ok and output.reason == "the job was purged; its output is gone"
        )
        again = (await ce.purge([h]))[h]
        assert again.ok and again.reason == "already released"
        assert "condor_transfer_data" not in condor.tools()
        assert "condor_rm" not in condor.tools()

    async def test_twice_is_harmless(self, ce: HTCondorCEBackend):
        h = (await ce.submit(SPEC)).handles[0]
        await ce.purge([h])
        again = (await ce.purge([h]))[h]
        assert again.ok and again.reason == "already released"

    async def test_purges_active_jobs_too(
        self, ce: HTCondorCEBackend, condor: FakeCondor
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id, code=2)
        assert (await ce.purge([h]))[h].ok
        assert h.id not in condor.queue


# --------------------------------------------------------------------------
# The working area
# --------------------------------------------------------------------------


class TestWorkingArea:
    async def test_is_private(self, ce: HTCondorCEBackend, workdir: Path):
        await ce.submit(SPEC)
        assert stat.S_IMODE(workdir.stat().st_mode) == 0o700

    async def test_a_link_in_its_place_is_refused(
        self, condor: FakeCondor, tmp_path: Path
    ):
        target = tmp_path / "elsewhere"
        target.mkdir()
        link = tmp_path / "work"
        link.symlink_to(target)
        async with make(condor, link) as backend:
            with pytest.raises(
                ConfigurationError, match="not a directory owned by this user"
            ):
                await backend.submit(SPEC)


class TestOutputEdgeCases:
    async def test_transfer_that_writes_nothing(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id)
        condor._transfer = lambda ids: ok("Fetching data files...\n")  # type: ignore[method-assign]
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert (
            not outcome.ok
            and outcome.reason == "condor_transfer_data returned no files"
        )

    async def test_log_outside_the_job_directory_is_not_the_log(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        h = (await ce.submit(SPEC)).handles[0]
        condor.finish(h.id, UserLog="/var/log/elsewhere.log")
        outcome = (await ce.get_output([h], tmp_path))[h]
        assert outcome.ok and outcome.value is not None
        assert outcome.value.log is None
        assert (
            "elsewhere.log" in outcome.value.files
        )  # written into the job directory by name


class TestStateless:
    """The backend keeps nothing about jobs between calls (IC-ADR-001 §1)."""

    async def test_no_job_is_remembered_after_collection(
        self, ce: HTCondorCEBackend, condor: FakeCondor, tmp_path: Path
    ):
        import gc

        sub = await ce.submit(SPEC, 3)
        for h in sub.handles.values():
            condor.finish(h.id)
        await ce.get_output(list(sub.handles.values()), tmp_path)
        gc.collect()
        assert len(ce._collecting) == 0

    async def test_one_job_collected_twice_at_once(
        self, condor: FakeCondor, workdir: Path, tmp_path: Path
    ):
        import asyncio

        transfers: list[str] = []  # "start" / "end", to see whether they overlap

        async def slow_transfer(
            argv: Sequence[str], env: Mapping[str, str], timeout: float
        ) -> CommandResult:
            if argv[0] != "condor_transfer_data":
                return await condor(argv, env, timeout)
            transfers.append("start")
            await asyncio.sleep(0.05)  # the second collection arrives meanwhile
            result = await condor(argv, env, timeout)
            transfers.append("end")
            return result

        provider = StaticCredentials([BearerToken(value=TOKEN)])
        backend = HTCondorCEBackend(
            settings(), provider, runner=slow_transfer, workdir=workdir
        )
        async with backend:
            h = (await backend.submit(SPEC)).handles[0]
            condor.finish(h.id)
            condor.outputs[h.id] = {"result.txt": b"42"}
            first, second = await asyncio.gather(
                backend.get_output([h], tmp_path / "a"),
                backend.get_output([h], tmp_path / "b"),
            )
        # Both succeed, one after the other: never two transfers into one directory.
        assert transfers == ["start", "end", "start", "end"]
        for outcome in (first[h], second[h]):
            assert outcome.ok and outcome.value is not None
            assert outcome.value.files["result.txt"].read_bytes() == b"42"

    async def test_a_new_backend_knows_the_jobs_of_another(
        self, condor: FakeCondor, workdir: Path, tmp_path: Path
    ):
        # Everything needed lives in the handle and at the CE, not in the backend.
        async with make(condor, workdir) as first:
            h = (await first.submit(SPEC)).handles[0]
        condor.finish(h.id, ExitCode=0)
        async with make(condor, workdir) as second:
            assert (await second.get_status([h]))[h] == JobStatus.DONE
            assert (await second.get_output([h], tmp_path))[h].ok
            assert (await second.purge([h]))[h].ok
