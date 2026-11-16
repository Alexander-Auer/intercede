"""Start the presentation app: ``pixi run demo`` (see demo/README.md)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    import uvicorn
except ImportError:  # run with a Python that is not the demo's environment
    sys.exit(
        "The presentation app runs in its own pixi environment. Start it with:\n"
        "  pixi run demo         (add --up to start the stacks first)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m demo", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8470)
    parser.add_argument(
        "--up", action="store_true", help="start every stack first, and wait for it"
    )
    parser.add_argument(
        "--down", action="store_true", help="stop every stack, and do nothing else"
    )
    args = parser.parse_args()
    if args.up or args.down:
        from demo.columns import compose_file, stacks

        files = {compose_file(s) for s in stacks() if compose_file(s).is_file()}
        for path in sorted(files):
            action = ["up", "--wait"] if args.up else ["down"]
            print("$ docker compose -f", path.relative_to(Path.cwd()), *action)
            subprocess.run(["docker", "compose", "-f", str(path), *action], check=False)  # noqa: S603, S607
        if args.down:
            return
    print(f"interCEde presentation on http://{args.host}:{args.port}")
    uvicorn.run("demo.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
