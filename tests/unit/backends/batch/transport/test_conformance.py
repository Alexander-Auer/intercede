"""What every transport does the same way: run, put and get, and how they fail.

Everything is checked through the transport itself (``cat``, ``test``), never
by looking at this host's files, so the same tests hold for a remote resource.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from intercede._backends.batch.protocols import Transport
from intercede.exceptions import ResourceUnavailableError

TIMEOUT = 30  # seconds; generous, since only the timeout tests rely on one

# Arguments a shell would change if it ever saw them unquoted.
AWKWARD = {
    "space": "two words",
    "outer-spaces": "  padded  ",
    "single-quote": "it's",
    "double-quote": 'say "hi"',
    "backslash": "back\\slash",
    "variable": "$HOME",
    "braces": "${PATH}",
    "backticks": "`id`",
    "substitution": "$(id)",
    "glob": "*",
    "pattern": "[a-z]?",
    "tilde": "~",
    "separators": "a;b&&c||d|e",
    "redirects": "<in >out 2>&1",
    "newline": "one\ntwo",
    "tab": "a\tb",
    "empty": "",
    "option": "-n",
    "end-of-options": "--",
    "comment": "#not a comment",
    "history": "!!",
    "percent": "%s%d",
    "unicode": "ünïcødé ✓",
}
# A file or directory name with as much of the above as a name can hold.
AWKWARD_NAME = "two words 'q' \"dq\" *glob* $HOME ~ !! ünï\nline"


async def run(transport: Transport, *argv: str) -> str:
    """Run a command that must succeed, and return what it printed."""
    result = await transport.run(argv, timeout=TIMEOUT)
    assert result.returncode == 0, result
    return result.stdout


async def exists(transport: Transport, path: str) -> bool:
    result = await transport.run(["test", "-e", path], timeout=TIMEOUT)
    return result.returncode == 0


def write(path: Path, content: bytes, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` and its content, by relative path."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class TestRun:
    async def test_returns_the_exit_code_and_what_was_printed(
        self, transport: Transport
    ):
        result = await transport.run(
            ["sh", "-c", "echo out; echo err >&2; exit 3"], timeout=TIMEOUT
        )
        assert (result.returncode, result.stdout, result.stderr) == (
            3,
            "out\n",
            "err\n",
        )

    @pytest.mark.parametrize("argument", AWKWARD.values(), ids=AWKWARD.keys())
    async def test_an_argument_arrives_exactly(
        self, transport: Transport, argument: str
    ):
        assert await run(transport, "printf", "%s\\0", argument) == f"{argument}\0"

    async def test_arguments_arrive_exactly_and_in_order(self, transport: Transport):
        printed = await run(transport, "printf", "%s\\0", *AWKWARD.values())
        assert printed.split("\0")[:-1] == list(AWKWARD.values())

    async def test_the_program_may_have_an_awkward_path(
        self, transport: Transport, remote: str, here: Path
    ):
        script = write(here / "script", b"#!/bin/sh\necho ran\n", mode=0o755)
        program = f"{remote}/{AWKWARD_NAME}/{AWKWARD_NAME}"
        await transport.put(script, program, timeout=TIMEOUT)
        assert await run(transport, program) == "ran\n"

    async def test_starts_in_the_home_directory(self, transport: Transport):
        # A bare ``cd`` goes home, wherever the resource keeps it.
        await run(transport, "sh", "-c", 'test "$(pwd -P)" = "$(cd && pwd -P)"')

    async def test_a_missing_program_exits_127(self, transport: Transport):
        result = await transport.run(["intercede-no-such-program"], timeout=TIMEOUT)
        assert result.returncode == 127

    async def test_what_cannot_be_executed_exits_126(
        self, transport: Transport, remote: str
    ):
        result = await transport.run([remote], timeout=TIMEOUT)  # a directory
        assert result.returncode == 126

    async def test_the_command_reads_no_input(self, transport: Transport):
        assert await run(transport, "cat") == ""  # returns at once, at end of input

    async def test_a_command_that_outlasts_its_timeout_fails(
        self, transport: Transport
    ):
        started = time.monotonic()
        with pytest.raises(ResourceUnavailableError, match="did not finish"):
            await transport.run(["sleep", "30"], timeout=0.5)
        assert time.monotonic() - started < 10


class TestCopy:
    async def test_a_file_arrives_with_its_content(
        self, transport: Transport, remote: str, here: Path
    ):
        source = write(here / "file", b"hello\n")
        await transport.put(source, f"{remote}/file", timeout=TIMEOUT)
        assert await run(transport, "cat", f"{remote}/file") == "hello\n"

    async def test_missing_parent_directories_are_created(
        self, transport: Transport, remote: str, here: Path
    ):
        source = write(here / "file", b"deep")
        await transport.put(source, f"{remote}/a/b/c/file", timeout=TIMEOUT)
        back = here / "x" / "y" / "file"
        await transport.get(f"{remote}/a/b/c/file", back, timeout=TIMEOUT)
        assert back.read_bytes() == b"deep"

    async def test_an_existing_file_is_replaced(
        self, transport: Transport, remote: str, here: Path
    ):
        for content in (b"first, and longer", b"second"):
            source = write(here / "file", content)
            await transport.put(source, f"{remote}/file", timeout=TIMEOUT)
        assert await run(transport, "cat", f"{remote}/file") == "second"

    async def test_permission_bits_are_kept_both_ways(
        self, transport: Transport, remote: str, here: Path
    ):
        script = write(here / "script", b"#!/bin/sh\n", mode=0o750)
        data = write(here / "data", b"", mode=0o640)
        await transport.put(script, f"{remote}/script", timeout=TIMEOUT)
        await transport.put(data, f"{remote}/data", timeout=TIMEOUT)
        await run(transport, "test", "-x", f"{remote}/script")
        await run(transport, "test", "!", "-x", f"{remote}/data")

        await transport.get(f"{remote}/script", here / "back", timeout=TIMEOUT)
        assert (here / "back").stat().st_mode & 0o777 == 0o750

    async def test_a_directory_makes_the_round_trip_whole(
        self, transport: Transport, remote: str, here: Path
    ):
        files = {
            "a.txt": b"a",
            "sub/b.txt": b"b",
            "sub/deeper/c.bin": bytes(range(256)),
        }
        for name, content in files.items():
            write(here / "tree" / name, content)
        (here / "tree" / "empty").mkdir()
        await transport.put(here / "tree", f"{remote}/tree", timeout=TIMEOUT)
        await transport.get(f"{remote}/tree", here / "back", timeout=TIMEOUT)
        assert tree(here / "back") == files
        assert (here / "back" / "empty").is_dir()

    async def test_put_copies_to_the_path_not_into_it(
        self, transport: Transport, remote: str, here: Path
    ):
        write(here / "source" / "file", b"x")
        await run(transport, "mkdir", f"{remote}/destination")
        await transport.put(here / "source", f"{remote}/destination", timeout=TIMEOUT)
        assert await exists(transport, f"{remote}/destination/file")
        assert not await exists(transport, f"{remote}/destination/source")

    async def test_get_copies_to_the_path_not_into_it(
        self, transport: Transport, remote: str, here: Path
    ):
        await run(
            transport,
            "sh",
            "-c",
            'mkdir "$1/source" && : > "$1/source/file"',
            "sh",
            remote,
        )
        (here / "destination").mkdir()
        await transport.get(f"{remote}/source", here / "destination", timeout=TIMEOUT)
        assert (here / "destination" / "file").is_file()
        assert not (here / "destination" / "source").exists()

    async def test_a_relative_path_is_relative_to_home(
        self, transport: Transport, here: Path
    ):
        source = write(here / "file", b"at home")
        await transport.put(source, "relative/file", timeout=TIMEOUT)
        await run(transport, "sh", "-c", 'test -f "$HOME/relative/file"')
        await transport.get("relative/file", here / "back", timeout=TIMEOUT)
        assert (here / "back").read_bytes() == b"at home"

    async def test_paths_may_hold_awkward_characters(
        self, transport: Transport, remote: str, here: Path
    ):
        source = write(here / AWKWARD_NAME / AWKWARD_NAME, b"odd")
        path = f"{remote}/{AWKWARD_NAME}/{AWKWARD_NAME}"
        await transport.put(source, path, timeout=TIMEOUT)
        assert await run(transport, "cat", path) == "odd"
        await transport.get(path, here / "back", timeout=TIMEOUT)
        assert (here / "back").read_bytes() == b"odd"

    async def test_getting_what_does_not_exist_fails(
        self, transport: Transport, remote: str, here: Path
    ):
        with pytest.raises(ResourceUnavailableError):
            await transport.get(f"{remote}/missing", here / "back", timeout=TIMEOUT)

    async def test_putting_what_does_not_exist_fails(
        self, transport: Transport, remote: str, here: Path
    ):
        with pytest.raises(ResourceUnavailableError):
            await transport.put(here / "missing", f"{remote}/file", timeout=TIMEOUT)

    async def test_putting_below_a_file_fails(
        self, transport: Transport, remote: str, here: Path
    ):
        source = write(here / "file", b"x")
        await transport.put(source, f"{remote}/file", timeout=TIMEOUT)
        with pytest.raises(ResourceUnavailableError):
            await transport.put(source, f"{remote}/file/below", timeout=TIMEOUT)


class TestLifecycle:
    async def test_closing_twice_is_harmless(self, transport: Transport):
        await transport.__aexit__(None, None, None)
        await transport.__aexit__(None, None, None)  # and the fixture closes it again
