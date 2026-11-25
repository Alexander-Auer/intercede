"""Start the class diagram app: ``python -m demo.desktop [--repo PATH] [--class NAME]``."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intercede-classes",
        description="interCEde's class diagram, read from a checkout.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(os.environ.get("INTERCEDE_REPO", Path.cwd())),
        help="the interCEde checkout to read (default: $INTERCEDE_REPO, or here)",
    )
    parser.add_argument(
        "--python",
        help="the Python that has intercede installed (default: the checkout's pixi environment)",
    )
    parser.add_argument(
        "--class", dest="open_class", help="open this class, as /classes#Name does"
    )
    args, qt_args = parser.parse_known_args(argv)

    repo = args.repo.resolve()
    if not (repo / "src" / "intercede").is_dir():
        parser.error(f"{repo} is not an interCEde checkout (no src/intercede)")
    dot = str(Path(sys.prefix) / "bin" / "dot")
    if not os.access(dot, os.X_OK):
        dot = shutil.which("dot") or parser.error("Graphviz's dot was not found")

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .window import Window
    from .work import checkout_python, icon_svg

    app = QApplication([sys.argv[0], *qt_args])
    app.setOrganizationName("intercede")
    app.setApplicationName("intercede-classes")
    app.setApplicationDisplayName("interCEde classes")
    app.setDesktopFileName(
        "intercede-classes"
    )  # Wayland matches the window to its launcher
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(icon_svg())))
    python = [args.python] if args.python else checkout_python(repo)
    window = Window(repo, python, dot, args.open_class)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
