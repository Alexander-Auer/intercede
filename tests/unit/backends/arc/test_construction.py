"""The ARC backend: shape, construction, and failures of a whole operation."""

from __future__ import annotations

import httpx
import pytest

from intercede import (
    AuthenticationError,
    InterCEdeError,
    JobHandle,
    JobID,
    ResourceUnavailableError,
    registry,
)
from intercede._backends.monolith.arc import ARCBackend
from intercede.auth import StaticCredentials
from intercede.protocols import Diagnosable, Killable, OutputRetriever, Purgeable

from ._fake_arc import ENDPOINT, SPEC, FakeARC, ScopedTokens, make, settings

HANDLE = JobHandle(backend="arc", resource=ENDPOINT, id=JobID("0123abcd"))


class TestShape:
    def test_submits_and_reports_status_only(self, arc: FakeARC):
        backend = make(arc)
        for capability in (Diagnosable, OutputRetriever, Killable, Purgeable):
            assert not isinstance(backend, capability)

    def test_construction_does_no_io(self, arc: FakeARC, tokens: ScopedTokens):
        make(arc, tokens)
        assert arc.requests == []
        assert tokens.asked == []

    def test_built_by_the_registry(self):
        built = registry.get_backend(
            {"kind": "arc", "endpoint": ENDPOINT}, StaticCredentials()
        )
        assert isinstance(built, ARCBackend)
        assert built.resource == ENDPOINT

    async def test_closing_closes_the_connections(self, arc: FakeARC):
        backend = make(arc)
        async with backend:
            await backend.get_status([HANDLE])
        assert arc.closed == 1
        await backend.__aexit__(None, None, None)  # closing twice is harmless
        with pytest.raises(InterCEdeError, match="closed"):
            await backend.submit(SPEC)

    async def test_reopened_after_closing(self, arc: FakeARC):
        backend = make(arc)
        async with backend:
            await backend.get_status([HANDLE])
        async with backend:
            await backend.get_status([HANDLE])
        assert arc.actions() == ["status", "status"]


class TestWholeOperationFailures:
    async def test_a_refused_token(self, ce: ARCBackend, arc: FakeARC):
        arc.refuse_token = True
        with pytest.raises(AuthenticationError, match="refused the token") as exc_info:
            await ce.submit(SPEC)
        assert "token:" not in str(exc_info.value)
        with pytest.raises(AuthenticationError):
            await ce.get_status([HANDLE])

    async def test_a_refused_connection_is_not_used_again(
        self, ce: ARCBackend, arc: FakeARC
    ):
        # A-REX refuses every later request on a connection it refused once.
        arc.refuse_token = True
        with pytest.raises(AuthenticationError):
            await ce.get_status([HANDLE])
        assert arc.closed == 1
        arc.refuse_token = False
        await ce.get_status([HANDLE])
        assert arc.actions() == ["status", "status"]

    @pytest.mark.parametrize("code", [401, 403])
    async def test_a_token_refused_with_another_status(
        self, ce: ARCBackend, arc: FakeARC, code: int
    ):
        arc.answer = httpx.Response(code)
        with pytest.raises(AuthenticationError):
            await ce.get_status([HANDLE])

    async def test_a_ce_that_fails_the_request(self, ce: ARCBackend, arc: FakeARC):
        arc.answer = httpx.Response(
            500, extensions={"reason_phrase": b"Internal Server Error"}
        )
        with pytest.raises(ResourceUnavailableError, match="500 Internal Server Error"):
            await ce.submit(SPEC)
        assert arc.closed == 0  # only a refused token drops the connection

    async def test_an_answer_that_is_not_arcs(self, ce: ARCBackend, arc: FakeARC):
        arc.answer = httpx.Response(201, text="<html>a proxy's error page</html>")
        with pytest.raises(ResourceUnavailableError, match="not ARC's"):
            await ce.get_status([HANDLE])

    async def test_a_ce_that_cannot_be_reached(self, tokens: ScopedTokens):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=request)

        backend = ARCBackend(settings(), tokens, transport=httpx.MockTransport(refuse))
        async with backend:
            with pytest.raises(ResourceUnavailableError, match="Connection refused"):
                await backend.submit(SPEC)

    async def test_a_provider_without_a_token(self, arc: FakeARC):
        async with ARCBackend(
            settings(), StaticCredentials(), transport=arc
        ) as backend:
            with pytest.raises(AuthenticationError):
                await backend.submit(SPEC)
        assert arc.requests == []
