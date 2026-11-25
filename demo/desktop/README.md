# The class diagram as a desktop app

The demo's class diagram (`/classes`) as an app of its own: every class in `intercede`, read from
a checkout, with everything the web page does. It is quicker because Graphviz lays out the diagram
and Qt draws it, and it stays current: it reads the code again whenever a file under
`src/intercede` changes or another branch is checked out.

## Install

From the checkout the app should read, on the demo branch (where `demo/` is):

```sh
python3 demo/desktop/install.py
```

This copies the app to `~/.local/share/intercede-classes/` with an environment of its own (Python,
PySide6 and Graphviz, from conda-forge through `pixi`), and adds the `intercede-classes` command and
*interCEde classes* to the app menu. Since the app is copied out of `demo/`, it keeps working when
another branch is checked out: it reads the code of whatever branch is checked out in that
checkout.

To update the app after a change to `demo/desktop/`, run the installer again.
`python3 demo/desktop/install.py --uninstall` removes it.

`intercede-classes --class SubmissionSpec` opens a class directly (as `/classes#SubmissionSpec`
does), and `--repo PATH` reads another checkout.

## What it does

The same as the web page: areas and their *only*, fields and methods, private members, "uses"
arrows, exceptions; find a class; a class's panel with its docstring, fields, methods and
relations; *Only its neighbours*; *Source*; *Centre*; drag to move, the wheel or **+/−** to zoom,
**0** to fit; the Mermaid text to copy, save or view. Settings are kept between runs.

New:

- **It follows the code.** A change to a file of the package is in the diagram about a second
  later; the caption says which branch it was read from, and when. **F5** reads it again by hand.
- **Open** opens the class in VS Code (`code -g`), or in the default editor.
- The theme follows the system, or is set to light or dark.
- A chosen class's arrows are drawn in blue and the others fade, so what it touches stands
  out. The source view has line numbers.

## Why it is quick

| | web page | app |
| --- | --- | --- |
| opening, diagram drawn | 2.5–2.8 s | 0.25 s (1 s the very first time) |
| a view not drawn before ("uses", members) | 1.2–3 s | about 0.2 s |
| a view drawn before, also after a restart | 1.2–3 s | 15–30 ms |
| one step of panning or zooming, painted | — | 7–14 ms |

Measured on the development laptop, with the 84 classes of the package.

- **Graphviz instead of Mermaid.** `dot` lays out the whole package in 0.06–0.2 s, where Mermaid
  takes 1–3 s, and it runs in its own process, so the window never waits for it.
- **Layouts are kept.** Each one is stored by its input (the classes, the switches and the box
  sizes) in `~/.cache/intercede/`, so a view seen before is drawn without laying it out again.
- **The last model read opens the window.** Reading the code takes about 0.8 s, because it imports
  the package; that happens behind the diagram, which is redrawn only if the code changed.
- **Qt draws it.** Each box is painted once per zoom level and then only moved.

## How it works

| file | what |
| --- | --- |
| `extract.py` | run by the checkout's own Python (`.pixi/envs/py313`): prints the model of `demo/classes.py`, and each class's source, as JSON |
| `diagram.py` | what is drawn, the Mermaid text, the Graphviz input and reading its layout; no Qt, tested on its own |
| `items.py` | the boxes, arrows and areas as Qt items |
| `style.py` | the look: the colours of `static/style.css`, fonts, icons, the stylesheet, the switch |
| `widgets.py` | the canvas and its floating controls, the code view, an area's row |
| `work.py` | reading the code and laying it out, in processes of their own, and the layouts kept |
| `window.py` | the window: what is drawn, the class panel, following the code |
| `install.py` | the installer |
| `pixi.toml` | the app's environment |

The model comes from the same `demo/classes.py` as the web page, so both show the same classes and
relations.

## Tests

```sh
pixi run -e demo demo-test                                                     # diagram.py
pixi run --manifest-path demo/desktop/pixi.toml pytest demo/tests/test_desktop_window.py
```
