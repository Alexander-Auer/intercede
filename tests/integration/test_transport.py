"""The transport suite of the unit tests, on the real thing: OpenSSH's sshd, or the container itself.

It runs on every stack of a composed backend, whichever way that stack reaches its
resource. The tests are the unit tests' own, so a transport is held to the same
behaviour against its test server and against a real one.
"""

from __future__ import annotations

import pytest

from tests.unit.backends.batch.transport.test_conformance import (
    TestCopy,
    TestLifecycle,
    TestRun,
)

pytestmark = [pytest.mark.scheduler, pytest.mark.slurm]

__all__ = ["TestCopy", "TestLifecycle", "TestRun"]
