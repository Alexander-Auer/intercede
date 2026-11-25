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
from pathlib import Path
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

    async def read(self, path: str, scopes: frozenset[str]) -> bytes | None:
        """Read a small document: a listing, or a file ARC keeps about a job.

        Returns:
            Its content, or None when the CE has nothing at ``path``.
        """
        response = await self._send("GET", path, scopes, missing_ok=True)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        content: bytes = response.content  # typed Any by httpx
        return content

    async def download(
        self, path: str, file: Path, limit: int, scopes: frozenset[str]
    ) -> int | None:
        """Write the file at ``path`` into a new ``file``, as it arrives.

        Args:
            path: A file of a session directory, or one ARC keeps about a job.
            file: Where to write it; must not exist.
            limit: How many bytes may arrive; the size is counted, not trusted.
            scopes: The token's.

        Returns:
            How many bytes were written, or None when the CE has no such file.

        Raises:
            ValueError: More than ``limit`` bytes arrived.
        """
        response = await self._send("GET", path, scopes, missing_ok=True, stream=True)
        try:
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            written = 0
            with file.open("xb") as out:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > limit:
                        raise ValueError(f"more than {limit} bytes")
                    out.write(chunk)
            return written
        finally:
            await response.aclose()

    async def tail(self, path: str, limit: int, scopes: frozenset[str]) -> bytes | None:
        """Read the last ``limit`` bytes of the file at ``path``, which may still grow.

        A-REX serves a range from an offset, not the last bytes, so the size is
        asked first. The range is only a saving: a CE that sends all of the file
        is read to its end, keeping no more than ``limit`` bytes.

        Returns:
            Its end, or None when the CE has no such file.
        """
        head = await self._send("HEAD", path, scopes, missing_ok=True)
        if head.status_code == httpx.codes.NOT_FOUND:
            return None
        try:
            size = int(head.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        start = max(0, size - limit)
        headers = {"Range": f"bytes={start}-"} if start else {}
        response = await self._send(
            "GET", path, scopes, missing_ok=True, stream=True, headers=headers
        )
        try:
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                del data[:-limit]  # whatever arrived beyond the last limit bytes
            return bytes(data)
        finally:
            await response.aclose()

    async def close(self) -> None:
        """Close the connections; the next request opens new ones. Idempotent."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        scopes: frozenset[str],
        *,
        missing_ok: bool = False,
        stream: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make a request; a streamed answer is the caller's to close.

        Args:
            method: The HTTP method.
            path: Relative to the interface.
            scopes: The token's.
            missing_ok: Return a 404 answer instead of raising.
            stream: Leave the body to be read by the caller.
            **kwargs: Passed on to httpx: content, json, headers.

        Returns:
            The answer: a success, or a 404 when ``missing_ok``.
        """
        token = await self._token(scopes)
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
        client = self._open()
        request = client.build_request(method, path, headers=headers, **kwargs)
        try:
            response = await client.send(request, stream=stream)
        except httpx.TransportError as exc:  # also a timeout, or a TLS failure
            raise ResourceUnavailableError(
                f"cannot reach {self._settings.endpoint}: {exc}"
            ) from exc
        if missing_ok and response.status_code == httpx.codes.NOT_FOUND:
            return response
        if _refused(response):
            await response.aclose()
            # The connection would refuse every later request as well.
            await self.close()
            raise AuthenticationError(
                f"{self._settings.endpoint} refused the token "
                f"({response.status_code} {response.reason_phrase})"
            )
        if response.is_error:
            await response.aclose()
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
