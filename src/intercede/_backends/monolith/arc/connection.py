"""The requests to one CE, each with the token of the operation that makes it.

A-REX decides who a connection belongs to at its first request. Once that
request is refused, or carries no token, every later request on the same
connection is refused too, whatever its token (ARC 7.2). So every request
carries a token, even to the public paths, and the connections are dropped
after a refusal: the next request opens a new one.

What a failure means for the whole operation is decided here: a refused token
is an ``AuthenticationError``, anything else that stops a request is a
``ResourceUnavailableError``. Neither message holds the token.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Sequence
from typing import Any

import httpx

from intercede._backends._arc import rest
from intercede._backends._arc.rest import JobAnswer
from intercede._backends.credentials import CredentialCache, credential_requirements
from intercede.auth.models import BearerToken
from intercede.config import ARCConfig
from intercede.exceptions import AuthenticationError, ResourceUnavailableError

# How long one step of a request (connecting, sending, waiting for an answer)
# may take, in seconds; not the whole request, so a large upload is not cut off.
TIMEOUT_S = 60

# How many files one operation moves at once.
PARALLEL_TRANSFERS = 4

# What A-REX answers, with HTTP status 500, when it refuses a token: the
# identity it found maps to no configuration.
_NO_CONFIGURATION = "User can't be assigned configuration"


class Connection:
    """The HTTPS connections to one CE, opened at the first request."""

    def __init__(
        self,
        settings: ARCConfig,
        credentials: CredentialCache,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Construction does no I/O: the client, and its TLS context, come later.
        self._settings = settings
        self._credentials = credentials
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def create(self, description: str, scopes: frozenset[str]) -> list[JobAnswer]:
        """Create a job per description in ``description``.

        Returns:
            One answer per description, in their order.
        """
        response = await self._send(
            "POST",
            "jobs?action=new",
            scopes,
            content=description.encode(),
            headers={"Content-Type": "application/xml"},
        )
        return self._answers(response)

    async def jobs(
        self, action: str, ids: Sequence[str], scopes: frozenset[str]
    ) -> list[JobAnswer]:
        """Ask the CE to do ``action`` ("status", "kill", ...) for each job of ``ids``.

        Returns:
            One answer per job, in the order of ``ids``.
        """
        response = await self._send(
            "POST", f"jobs?action={action}", scopes, json=rest.jobs_body(ids)
        )
        return self._answers(response)

    async def upload(
        self, job_id: str, name: str, content: bytes, scopes: frozenset[str]
    ) -> None:
        """Write a file into a job's session directory."""
        await self._send(
            "PUT",
            rest.session_path(job_id, name),
            scopes,
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )

    async def close(self) -> None:
        """Close the connections; the next request opens new ones. Idempotent."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _send(
        self, method: str, path: str, scopes: frozenset[str], **kwargs: Any
    ) -> httpx.Response:
        token = await self._token(scopes)
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
        try:
            response = await self._open().request(
                method, path, headers=headers, **kwargs
            )
        except httpx.TransportError as exc:  # also a timeout, or a TLS failure
            raise ResourceUnavailableError(
                f"cannot reach {self._settings.endpoint}: {exc}"
            ) from exc
        if _refused(response):
            # The connection would refuse every later request as well.
            await self.close()
            raise AuthenticationError(
                f"{self._settings.endpoint} refused the token "
                f"({response.status_code} {response.reason_phrase})"
            )
        if response.is_error:
            raise ResourceUnavailableError(
                f"{self._settings.endpoint} answered {response.status_code} "
                f"{response.reason_phrase} to {method} {path.partition('?')[0]}"
            )
        return response

    def _answers(self, response: httpx.Response) -> list[JobAnswer]:
        try:
            return rest.parse_jobs(response.text)
        except ValueError as exc:
            raise ResourceUnavailableError(
                f"{self._settings.endpoint} sent an answer that is not ARC's: {exc}"
            ) from exc

    async def _token(self, scopes: frozenset[str]) -> str:
        requirements = credential_requirements(self._settings, scopes)
        assert requirements is not None  # an ARC CE always asks for a token
        token = await self._credentials.fresh(requirements)
        assert isinstance(token, BearerToken)  # the cache checked the kind
        return token.value.get_secret_value()

    def _open(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=rest.api_url(self._settings.endpoint),
                headers={"Accept": "application/json"},
                timeout=TIMEOUT_S,
                verify=trusted_cas(),
                transport=self._transport,
            )
        return self._client


def trusted_cas() -> ssl.SSLContext:
    """Trust the system's CAs, and the grid's in ``X509_CERT_DIR`` when it is set.

    A CE's host certificate is usually signed by a grid CA (IGTF), which the
    system does not know; grid hosts keep those in ``X509_CERT_DIR``, by
    default ``/etc/grid-security/certificates``.
    """
    context = ssl.create_default_context()
    if directory := os.environ.get("X509_CERT_DIR"):
        context.load_verify_locations(capath=directory)
    return context


def _refused(response: httpx.Response) -> bool:
    """Tell whether the CE refused the request's token."""
    if response.status_code in (401, 403):
        return True
    reason: str = response.reason_phrase  # typed Any by httpx
    return response.status_code == 500 and reason == _NO_CONFIGURATION
