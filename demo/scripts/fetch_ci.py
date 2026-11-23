"""Snapshot the integration workflow's results into demo/data/ci.json, for scene 8.

Run it before the talk, with the network: ``pixi run -e demo python demo/scripts/fetch_ci.py``.
The demo itself never fetches anything; it shows the snapshot and its date.

It reads the GitHub REST API: the latest runs of ``integration.yml`` on each
repository, and the jobs of each run, which CI names ``stack / config / version``
from tests/integration/stacks.yml. Both repositories are public, so no token is
needed; a token (``GITHUB_TOKEN``, or the git-ignored file demo/.github-token)
only raises the rate limit.

Without a token GitHub allows 60 requests an hour per address, and an address
may be shared by a whole site. So a run that has finished is read once: its jobs
are taken from the previous snapshot, and a refresh costs one request per
repository and one per new run. Before reading anything, the script checks the
budget (which costs nothing) and stops with the time it resets if too little is
left, leaving the snapshot as it was.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.integration.generate_matrix import (  # noqa: E402
    build_matrix,
    is_ready,
    load_manifest,
)

API = "https://api.github.com"
REPOSITORIES = ("DIRACGrid/intercede", "Alexander-Auer/intercede")
WORKFLOW = "integration.yml"
OUT = ROOT / "demo" / "data" / "ci.json"
# Where the app looks for a token when GITHUB_TOKEN is not set; git-ignored.
TOKEN_FILE = ROOT / "demo" / ".github-token"


# Conclusions of a run that has finished: its jobs never change again.
FINISHED = {
    "success",
    "failure",
    "cancelled",
    "skipped",
    "timed_out",
    "neutral",
    "action_required",
    "startup_failure",
}


class RateLimitError(Exception):
    """GitHub refuses more requests from this address until ``reset``."""

    def __init__(self, remaining: int, reset: int) -> None:
        self.remaining, self.reset = remaining, reset


def get(path: str) -> Any:
    request = urllib.request.Request(  # noqa: S310  (API is a fixed https URL)
        f"{API}{path}", headers={"Accept": "application/vnd.github+json"}
    )
    if secret := token():
        request.add_header("Authorization", f"Bearer {secret}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  (a fixed https URL)
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise RateLimitError(
                0, int(exc.headers.get("X-RateLimit-Reset", "0"))
            ) from None
        raise


def token() -> str | None:
    """A GitHub token, from GITHUB_TOKEN or demo/.github-token; None to ask without one."""
    if secret := os.environ.get("GITHUB_TOKEN"):
        return secret.strip()
    if TOKEN_FILE.is_file():
        return TOKEN_FILE.read_text().strip() or None
    return None


def budget() -> tuple[int, int]:
    """Requests left this hour, and when the count resets; asking costs nothing."""
    core = get("/rate_limit")["resources"]["core"]
    return core["remaining"], core["reset"]


def listed(repository: str, count: int) -> list[dict[str, Any]]:
    path = f"/repos/{repository}/actions/workflows/{WORKFLOW}/runs?per_page={count}"
    return list(get(path)["workflow_runs"])


def summary(run: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": run["id"],
        "number": run["run_number"],
        "branch": run["head_branch"],
        "event": run["event"],
        "created": run["created_at"],
        "url": run["html_url"],
        "conclusion": run["conclusion"] or run["status"],
        # job name "<stack> / <config> / <version>" -> its conclusion
        "jobs": {job["name"]: job["conclusion"] or job["status"] for job in jobs},
    }


def previous() -> dict[int, dict[str, Any]]:
    """The finished runs of the last snapshot, by id: no need to ask about them again."""
    if not OUT.is_file():
        return {}
    data = json.loads(OUT.read_text())
    return {
        run["id"]: run
        for runs in data.get("repositories", {}).values()
        for run in runs
        if run.get("conclusion") in FINISHED
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=12, help="runs per repository")
    args = parser.parse_args()
    try:
        snapshot = fetch(args.runs)
    except RateLimitError as limited:
        when = datetime.fromtimestamp(limited.reset).astimezone().strftime("%H:%M")
        sys.exit(
            f"GitHub's rate limit for this address is used up until {when} "
            f"({limited.remaining} requests left). Try again then, or set GITHUB_TOKEN "
            "to any GitHub token, which raises the limit to 5000 an hour. "
            f"{OUT.relative_to(ROOT)} is unchanged."
        )
    save(snapshot)
    print(f"wrote {OUT.relative_to(ROOT)}")


def save(snapshot: dict[str, Any]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=1) + "\n")


def fetch(count: int) -> dict[str, Any]:
    known = previous()
    remaining, reset = budget()
    if remaining < len(REPOSITORIES):
        raise RateLimitError(remaining, reset)
    runs = {repository: listed(repository, count) for repository in REPOSITORIES}
    asking = [
        run
        for listed_runs in runs.values()
        for run in listed_runs
        if run["id"] not in known
    ]
    remaining -= len(REPOSITORIES)
    print(
        f"{len(asking)} new or unfinished runs to read, {remaining} requests left this hour"
    )
    if len(asking) > remaining:
        raise RateLimitError(remaining, reset)
    stacks = load_manifest()
    snapshot: dict[str, Any] = {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "workflow": WORKFLOW,
        # Every job CI makes from stacks.yml, and the stacks it skips for now.
        "axes": [
            f"{j['stack']} / {j['config']} / {j['version']}"
            for j in build_matrix([entry for entry in stacks if is_ready(entry)])
        ],
        "not_ready": [entry["id"] for entry in stacks if not is_ready(entry)],
        "repositories": {},
    }
    for repository, listed_runs in runs.items():
        print(f"{repository}: the last {len(listed_runs)} runs of {WORKFLOW}")
        snapshot["repositories"][repository] = [
            known.get(run["id"])
            or summary(
                run,
                get(f"/repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100")[
                    "jobs"
                ],
            )
            for run in listed_runs
        ]
    return snapshot


if __name__ == "__main__":
    main()
