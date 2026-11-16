"""Start the presentation app: ``pixi run demo`` (see demo/README.md)."""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m demo", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8470)
    args = parser.parse_args()
    print(f"interCEde presentation on http://{args.host}:{args.port}")
    uvicorn.run("demo.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
