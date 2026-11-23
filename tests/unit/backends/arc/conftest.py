"""Fixtures for the ARC backend tests."""

from __future__ import annotations

import pytest

from ._fake_arc import FakeARC, ScopedTokens, make


@pytest.fixture
def arc() -> FakeARC:
    return FakeARC()


@pytest.fixture
def tokens() -> ScopedTokens:
    return ScopedTokens()


@pytest.fixture
async def ce(arc: FakeARC, tokens: ScopedTokens):
    async with make(arc, tokens) as backend:
        yield backend
