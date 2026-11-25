# interCEde, live: a presentation app

A local web app that runs the real interCEde library and makes visible what it does, driven like a
slide deck. It is not part of the package: it lives outside `src/`, is left out of the sdist and
the wheel, and nothing in CI runs it.

## Start

```sh
pixi run demo          # http://127.0.0.1:8470
pixi run demo --up     # start every stack first, then the app
pixi run demo --down   # stop every stack
```

The app works fully offline: no CDN and no fonts to download. Its one JavaScript library,
Mermaid for the class diagram, is shipped in `static/vendor/`. Without
Docker it still runs: the Fake column, the static scenes, the dry run (scene 3) and Replay need
nothing else.

### The stacks

There is a live column for every stack in `tests/integration/stacks.yml` whose backend this
version of interCEde provides, built from the stack's own `intercede-client.toml`. The stacks are
started the way `docs/dev/how-to/run-integration-tests-locally.md` starts them
(`docker compose -f tests/integration/stacks/<stack>/compose.yml up --wait`), or all at once with
`pixi run demo --up`.

A column whose stack is not running says so, shows that command and has a button that runs it. The
app checks every 5 s and rebuilds a column when its stack comes back, with the credentials the
stack minted at its start.

A backend added to interCEde, with its stack, appears on the board, in the dry run and in every
scene without a change to the demo: nothing in the demo names a backend
(`demo/tests/test_generic.py` checks that).

## Presenting

- The tabs under the title name every scene; the buttons beside them say which scene comes before
  and after. **←/→** (or Space, Page Up/Down) step, **1–9** jump, and **Esc** (or ☰) shows the
  whole talk with each scene's caption.
- Each scene has an address by name (`#job-board`, `#normalisation`), and `?theme=dark` picks the
  theme, so a bookmark opens the talk where it should.
- The status bar shows which stacks are reachable, the mode, **Record** and **Reset**. Reset
  purges every job it can, then clears the board and the traces.
- **Modes:** *Live* shows every column; *Fake* shows only the in-process Fake (no Docker needed);
  *Replay* plays a recording through the same views.

Every view has a badge saying where its data comes from: **LIVE** (a real backend), **FAKE**
(`FakeBackend`, in process), **REPLAY** (a recorded live run), **DRY RUN** (the real code, with the
resource replaced by a recorder), **FROM CODE** (read from the library's source) or **SNAPSHOT**
(the CI results, as of the time shown). Nothing simulated looks live: while a recording plays,
every badge says REPLAY.

### Suggested order for a 15-minute talk

| min | scene | what to do |
|---|---|---|
| 0–2 | 1 Architecture | The problem: one WMS, many kinds of resource. Click `submit()` and `Diagnosable` to show the essential and the optional capabilities, straight from the docstrings. |
| 2–3 | 2 Composition | N transports + M schedulers, not N × M classes. Green cells run in CI. |
| 3–5 | 3 Translation | Edit the spec: the same one becomes what each backend sends, in its own dialect. Click *In a container*, *URL input*, *Outputs by pattern*: each backend refuses what it cannot honour, before anything runs. |
| 5–9 | 4 Job board | Submit the quick job to every column. Same consumer code, a dialect per backend. Submit *30 s with progress*; on the Fake, select one and press Diagnostics; on a real backend, the same button says the backend is not `Diagnosable`. Get output, then kill. |
| 9–10 | 5 Trace | *Compare the latest submit on two backends*: the top is the same, the bottom is completely different. Click an arrow to show the argv, the staged files, the token request. |
| 10–11 | 6 Normalisation | *Status*: each resource's own answer on the left (a tool's output, a response body), the same `Mapping[JobHandle, JobStatus]` on the right. Then *Submit* and *Kill*. |
| 11–12 | 7 Lifecycle | Show the handle going into the database. *Restart interCEde*: a new backend finds every job from the stored handles alone. *Forged handle*: UNKNOWN, never dropped. |
| 12–14 | 8 Partial failure | *Some copies refused*, then *Whole resource down* (raises) against *Per-job failures* (does not), then *Kill running and finished jobs* on a real backend. |
| 14–15 | 9 Verified | Press *Check the latest CI runs*: every stack × version runs the same contract suite on every pull request. |

If Docker or the network lets you down, switch to **Replay**: `demo/recordings/` holds a complete
run of scenes 4–8 against the live stacks.

### The class diagram

**Classes** in the bar under the title opens `/classes`: every class in `intercede`, drawn with
Mermaid from what `demo/classes.py` reads in the package when the app starts. Nothing in it is
written by hand, so a class added to the library is on it at the next start.

- **Arrows:** inherits from (a base that is an interCEde class), implements (a protocol whose
  methods the class has with the same parameters, which is how a type checker sees it), holds (a
  field or an attribute `__init__` sets) and uses (a method's parameter or return value).
- **Boxes** are grouped by package. A box's stereotype says what it is: protocol, model,
  dataclass, enumeration, exception.
- **Looking around:** drag to move, wheel or **+/−** to zoom, **0** to fit. Click a class for its
  docstring, fields, methods and relations; *Only its neighbours* draws that class and what it
  touches, with room for the detail; *Source* shows its code. `/classes#SubmissionSpec` opens a
  class directly.
- **Filters:** areas (hover one for *only*), fields and methods, private members, "uses" arrows,
  exceptions.
- **Mermaid:** *Copy*, *Download .mmd* and *View* give the Mermaid text of what is drawn, to paste
  into a document or a GitHub comment.

The same diagram is also a desktop app, quicker and read again whenever the code changes: see
[desktop/README.md](desktop/README.md).

## Recording and replay

**Record** saves every event the browser sees, from a full snapshot on, until you press it again,
into `demo/recordings/<date-time>.json.gz`. **Replay** plays one back at 0.5–8×, through the same
views, with the badge forced to REPLAY. While a recording plays, live events reach no browser and
every action that would reach a backend is refused (HTTP 409).

The background `get_status` polls are most of the events, and most repeat the one before: a poll
is kept only when its answer changed, with the commands it ran, so scene 6 replays the moments a
status changed. Collected output files are not in a recording; replayed, their links only open
what is still on this machine.

## The CI scene

Scene 9 shows the last runs of `integration.yml` on DIRACGrid/intercede and
Alexander-Auer/intercede, and the jobs CI names `stack / config / version` from `stacks.yml`.
**Check the latest CI runs** reads them from the GitHub REST API through the server, and can repeat
it every minute while the scene is shown. Each check is kept in `demo/data/ci.json`, so without the
network the scene shows the last one, with its time. The same check from a terminal:

```sh
pixi run -e demo python demo/scripts/fetch_ci.py
```

The token is read from `GITHUB_TOKEN`, or from `demo/.github-token` (one line, git-ignored, best
`chmod 600`). It never leaves this host: the server registers it with the redactor before it asks
GitHub anything, so no event, view or recording holds it. A fine-grained token with read access to
public repositories is enough; revoke it after the talk.

Without a token GitHub allows 60 requests an hour per address, and an institute's address is
shared by everyone behind it. So a run that has finished is read only once: a refresh costs one
request per repository and one per new run. If too few are left, the script says when the limit
resets and leaves the snapshot as it was; `GITHUB_TOKEN=<any GitHub token>` raises the limit to
5000 an hour.

## How it is instrumented

Nothing under `src/` is changed or reimplemented. Every status, handle, command line, request and
outcome shown in Live comes from a real call into interCEde. Which backends there are, and how each
is built, comes from the stacks' configuration, the registry and the configuration models
(`demo/build.py`). The demo watches the real objects at these seams:

- **The backend**, as the consumer sees it. Since Python 3.12, `isinstance` on a
  `runtime_checkable` protocol looks attributes up statically, so a delegating `__getattr__`
  would satisfy no protocol at all. `traced_backend` therefore builds the wrapper class per
  backend, with exactly the operations the backend has, and the board's capability chips are
  those `isinstance` checks.
- **A composed backend** (a configuration with a transport and a scheduler): the registry's own
  classes, composed the way `registry._build` composes them, after the same `registry._checked`
  protocol checks, with the transport and the scheduler wrapped at their protocols. The traced
  transport records every argv and every file `put` stages; the traced scheduler records each
  command it builds and each parse.
- **Any other backend** is built by `registry.get_backend`, exactly as a consumer builds it. What
  it sends to its resource is seen where it leaves the process (`demo/doors.py`): a program it
  runs (its argv, the files named on it, the environment it chose, its output) and an HTTP request
  it makes (its method, path and body, and the answer). The watchers change nothing on the way.
  A backend that reaches its resource another way still runs; its calls below the provider are
  just not drawn.
- **The credential provider:** `TracingProvider` around `StaticCredentials`, holding what the
  stack minted, read with the integration suite's own readers (`tests/integration/conftest.py`).
  It shows when a backend asks, for what kind and with which scopes. A TLS stack's CA is trusted
  through `X509_CERT_DIR`, as the integration suite trusts it.
- **A stack marked `exec_in_container`** is run where the integration suite runs it.
  `demo/agent.py` is started with `docker compose exec` in the stack's container, builds that
  stack's backend the same way, and passes its trace steps back over stdin/stdout. On this host
  `AgentBackend` forwards the consumer's calls. The payload script and the collected files cross
  that pipe too, since the container has neither.
- **Scene 3 (DRY RUN):** the real `submit()` of each backend runs, built the same way, with the
  resource replaced: a composed backend's transport by a recorder, and for any other backend the
  doors stand in (`demo/dryrun.py`). Every call is recorded and answered with nothing, so
  `submit()` stops at the first answer it needs, after sending what it would send. While a dry run
  goes, no connection can leave the process at all (an audit hook).

What is not read from the code: the payload scripts, the scenario scripts (`demo/scenarios.py`),
and where the state machine's states are drawn (the states themselves come from `JobStatus`; one
missing from the layout is still drawn).

### Secrets

Redaction happens on the bus, before any event leaves the server or reaches a recording
(`demo/events.py`), never in the browser. Every credential the provider hands out and every
per-copy secret a `submit` is given is registered first, and is replaced in every string, also in
base64 and per PEM line. Files under a copy's directory, which hold its secrets, are listed by name
only. The home directory is shown as `~`. `demo/tests/test_recording.py` checks a recording for
all of these.

## Tests

```sh
pixi run -e demo demo-test
```

They check that the tracing wrappers pass exactly the protocol checks of what they wrap, that a
recording round-trips and replays behind the gate, that no credential or per-copy secret reaches a
recording, and that the demo names no backend: every provided stack is a column and a dry-run
target, without the demo knowing it. The library's own suite does not collect them (`testpaths = ["tests"]`).

## What the demo shows about the library

- The capability chips are the protocols each backend satisfies: a backend without
  `Diagnosable` shows that instead of being called.
- "SSH via a jump host" is the `jump_host` field of a composed configuration, not a transport of
  its own; scene 2 marks it implemented until a CI stack uses it.
- What a backend cannot honour is refused at submit, before anything is sent; scene 3 shows each
  backend's refusal next to the others' translations.

## Layout

| path | what |
|---|---|
| `app.py`, `__main__.py` | the server: one event stream (SSE), the presenter's actions |
| `events.py` | the bus, and the redaction on it |
| `tracing.py`, `doors.py` | the tracing wrappers, and the watchers where a backend reaches its resource |
| `build.py` | which backends there are, and how each is built, from the stacks and the code |
| `columns.py`, `agent.py` | the columns on the board, and running one inside its stack's container |
| `consumer.py` | the consumer code shown on the board, the same for every backend |
| `board.py`, `payloads.py`, `scenarios.py` | the board, its payloads, the scripted scenes |
| `views.py`, `dryrun.py` | the views read from the code, and the dry run |
| `classes.py` | the class diagram's model: classes, inheritance, protocols, fields, read from the package |
| `desktop/` | the class diagram as a desktop app (Qt and Graphviz), installed with `desktop/install.py` |
| `recording.py`, `recordings/` | record and replay, and the committed live run |
| `scripts/fetch_ci.py`, `data/ci.json` | reading the CI runs from GitHub, and the last result |
| `static/` | the browser side: plain ES modules, SVG and CSS, no build step |
| `static/vendor/` | third-party code shipped as it is: Mermaid 11.4.1 and its licence |
