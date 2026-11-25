"""What only the ARC stack has to show: that a client on this host can reach and use its CE.

Everything else is the shared suite.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.remote, pytest.mark.arc]


@pytest.fixture
def folder(client_config: dict[str, Any], stack_dir: Path) -> Path:
    """Return the credentials folder the stack wrote at start."""
    path = stack_dir / client_config["credentials"]["path"]
    if not (path / "token").is_file():
        pytest.fail(f"{path / 'token'} is missing: is the stack up?")
    return path


def get(url: str, folder: Path, token: str | None = None) -> Any:
    """GET a JSON document from the CE, trusting it as a grid client does."""
    # The CA in the X509_CERT_DIR layout, checked as strictly as Python checks by default
    # (3.13 and later refuse a CA without the keyUsage extension).
    context = ssl.create_default_context(capath=folder / "certificates")
    # The URL is the stack's own endpoint, from its intercede-client.toml.
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, context=context, timeout=30) as response:  # noqa: S310
        return json.load(response)


def test_the_ce_is_trusted_and_speaks_rest_1_1(
    client_config: dict[str, Any], folder: Path
):
    versions = get(f"{client_config['backend']['endpoint']}/rest", folder)
    assert "1.1" in versions["version"]


def test_the_ce_lets_the_stack_token_in(client_config: dict[str, Any], folder: Path):
    token = (folder / "token").read_text().strip()
    jobs = get(f"{client_config['backend']['endpoint']}/rest/1.1/jobs", folder, token)
    assert isinstance(jobs, dict)
