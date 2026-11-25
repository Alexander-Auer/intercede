"""Print the class diagram's model as JSON: run by the checkout's own Python, not the app's.

The diagram is read by importing the package (``demo/classes.py``), so it runs in an
environment that has ``intercede`` installed from the checkout. The app starts it as
a separate process each time the code changes: a fresh import, of the branch that
is checked out now.

Usage: ``python extract.py <path of classes.py>``
"""

from __future__ import annotations

import importlib.util
import json
import sys


def main() -> int:
    spec = importlib.util.spec_from_file_location("_diagram_classes", sys.argv[1])
    assert spec is not None and spec.loader is not None
    classes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(classes)
    model = classes.model()
    sources = {}
    for c in model["classes"]:
        shown = classes.source(c["id"])
        if shown is not None:
            sources[c["id"]] = shown["source"]
    json.dump({"model": model, "sources": sources}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
