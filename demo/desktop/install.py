"""Install the class diagram as an app of this desktop, for this user; or remove it.

``python demo/desktop/install.py [--uninstall]``, from the checkout the app is to read.
Any Python 3.11 or newer runs it: it only copies files and calls ``pixi``.

The app is copied out of the checkout (``demo/`` only exists on the demo branch), with
an environment of its own (PySide6 and Graphviz). The checkout it reads is the one
this was run from; the app reads whichever branch is checked out there.

What it writes, all under the user's home (XDG paths):

- ``~/.local/share/intercede-classes/``: the app, ``classes.py`` and its environment
- ``~/.local/bin/intercede-classes``: the command
- ``~/.local/share/applications/intercede-classes.desktop``: the launcher in the app menu
- ``~/.local/share/icons/hicolor/scalable/apps/intercede-classes.svg``: its icon
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "intercede-classes"
HERE = Path(__file__).resolve().parent
PACKAGE = "intercede_classes"  # the app's package, once installed
APP_FILES = (
    "__init__.py",
    "__main__.py",
    "diagram.py",
    "extract.py",
    "items.py",
    "journey.py",
    "style.py",
    "widgets.py",
    "trace.py",
    "window.py",
    "work.py",
)


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def paths() -> dict[str, Path]:
    data = data_home()
    return {
        "home": data / NAME,
        "command": Path.home() / ".local" / "bin" / NAME,
        "launcher": data / "applications" / f"{NAME}.desktop",
        "icon": data / "icons" / "hicolor" / "scalable" / "apps" / f"{NAME}.svg",
    }


def pixi() -> str:
    found = shutil.which("pixi") or str(Path.home() / ".pixi" / "bin" / "pixi")
    if not os.access(found, os.X_OK):
        sys.exit("pixi was not found: https://pixi.sh installs it")
    return found


def install(repo: Path) -> None:
    where = paths()
    home = where["home"]
    package = home / PACKAGE
    package.mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        shutil.copy2(HERE / name, package / name)
    shutil.copy2(HERE.parent / "classes.py", package / "classes.py")
    shutil.copy2(HERE.parent / "static" / "icon.svg", package / "icon.svg")
    shutil.copy2(HERE / "pixi.toml", home / "pixi.toml")
    (home / "launch.py").write_text(
        '"""Start the installed app; its package is next to this file."""\n'
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        f"from {PACKAGE}.__main__ import main  # noqa: E402\n\n"
        "raise SystemExit(main())\n"
    )

    print(f"Creating the app's environment in {home} (PySide6, Graphviz)…")
    manifest = str(home / "pixi.toml")
    subprocess.run([pixi(), "install", "--manifest-path", manifest], check=True)  # noqa: S603  (our own manifest)
    python = home / ".pixi" / "envs" / "default" / "bin" / "python"

    command = where["command"]
    command.parent.mkdir(parents=True, exist_ok=True)
    command.write_text(
        "#!/bin/sh\n"
        "# interCEde's class diagram; written by demo/desktop/install.py.\n"
        f'exec "{python}" "{home / "launch.py"}" --repo "{repo}" "$@"\n'
    )
    command.chmod(0o755)

    where["icon"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE.parent / "static" / "icon.svg", where["icon"])
    where["launcher"].parent.mkdir(parents=True, exist_ok=True)
    where["launcher"].write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=interCEde classes\n"
        f"Comment=interCEde's class diagram, read from {repo}\n"
        f'Exec="{command}"\n'
        f"Icon={NAME}\n"
        "Terminal=false\n"
        "Categories=Development;\n"
        f"StartupWMClass={NAME}\n"
    )
    if (tool := shutil.which("update-desktop-database")) is not None:
        folder = str(where["launcher"].parent)
        subprocess.run([tool, folder], check=False)  # noqa: S603  (our own folder)
    print(
        f"Installed. Start it from the app menu (interCEde classes) or with `{NAME}`."
    )
    print(f"It reads {repo}, whichever branch is checked out there.")


def uninstall() -> None:
    where = paths()
    shutil.rmtree(where["home"], ignore_errors=True)
    for key in ("command", "launcher", "icon"):
        where[key].unlink(missing_ok=True)
    print(
        "Removed. Its settings and kept layouts are in ~/.config/intercede and ~/.cache/intercede."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--uninstall", action="store_true", help="remove the app")
    args = parser.parse_args()
    if args.uninstall:
        uninstall()
        return 0
    repo = HERE.parents[1]
    if not (repo / "src" / "intercede").is_dir():
        sys.exit(f"{repo} is not an interCEde checkout")
    install(repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
