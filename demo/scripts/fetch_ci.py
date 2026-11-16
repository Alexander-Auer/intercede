"""Snapshot the integration workflow's results into demo/data/ci.json, for scene 8.

Run it before the talk, with the network: ``pixi run -e demo python demo/scripts/fetch_ci.py``.
The demo itself never fetches anything; it shows the snapshot and its date.

It reads the GitHub REST API: the latest runs of ``integration.yml`` on each
repository, and the jobs of each run, which CI names ``stack / config / version``
from tests/integration/stacks.yml. Both repositories are public, so no token is
needed; ``GITHUB_TOKEN``, when set, only raises the rate limit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def get(path: str) -> Any:
    request = urllib.request.Request(  # noqa: S310  (API is a fixed https URL)
        f"{API}{path}", headers={"Accept": "application/vnd.github+json"}
    )
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  (a fixed https URL)
        return json.load(response)


def runs(repository: str, count: int) -> list[dict[str, Any]]:
    listed = get(
        f"/repos/{repository}/actions/workflows/{WORKFLOW}/runs?per_page={count}"
    )["workflow_runs"]
    result = []
    for run in listed:
        jobs = get(f"/repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100")[
            "jobs"
        ]
        result.append(
            {
                "id": run["id"],
                "number": run["run_number"],
                "branch": run["head_branch"],
                "event": run["event"],
                "created": run["created_at"],
                "url": run["html_url"],
                "conclusion": run["conclusion"] or run["status"],
                # job name "htcondor / basic / lts" -> its conclusion
                "jobs": {
                    job["name"]: job["conclusion"] or job["status"] for job in jobs
                },
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=12, help="runs per repository")
    args = parser.parse_args()
    stacks = load_manifest()
    snapshot = {
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
    for repository in REPOSITORIES:
        print(f"{repository}: reading the last {args.runs} runs of {WORKFLOW}")
        snapshot["repositories"][repository] = runs(repository, args.runs)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
