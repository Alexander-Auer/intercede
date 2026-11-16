# interCEde, live: a presentation app

A local web app that runs the real interCEde library and makes visible what it does, driven like a
slide deck. It is not part of the package: it lives outside `src/`, is left out of the sdist and
the wheel, and nothing in CI runs it.

## Start

```sh
pixi run demo                          # http://127.0.0.1:8470
pixi run -e demo python -m demo --up   # start every stack first, then the app
pixi run -e demo python -m demo --down # stop every stack
```

The app works fully offline: no CDN, no fonts to download, no JavaScript library at all. Without
Docker it still runs: the Fake column, the static scenes, the dry run (scene 3) and Replay need
nothing else.

### The stacks

The live columns use the integration stacks, started the way
`docs/dev/how-to/run-integration-tests-locally.md` starts them:

```sh
docker compose -f tests/integration/stacks/htcondor/compose.yml up --wait
docker compose -f tests/integration/stacks/slurm/compose.yml up --wait   # ssh-slurm and local-slurm
```

A column whose stack is not running says so, shows that command and has a button that runs it. The
app checks every 5 s and rebuilds a column when its stack comes back, with the credentials the
stack minted at its start.

## Presenting

- **←/→** (or Page Up/Down) step through the scenes, **1–8** jump. The URL keeps the scene (`#4`),
  and `?theme=dark` picks the theme, so a bookmark opens the talk where it should.
- The status bar shows which stacks are reachable, the mode, **Record** and **Reset**. Reset
  purges every job it can, then clears the board and the traces.
- **Modes:** *Live* shows every column; *Fake* shows only the in-process Fake (no Docker needed);
  *Replay* plays a recording through the same views.

Every view has a badge saying where its data comes from: **LIVE** (a real backend), **FAKE**
(`FakeBackend`, in process), **REPLAY** (a recorded live run), **DRY RUN** (the real code, with the
resource replaced by a recorder), **FROM CODE** (read from the library's source) or **SNAPSHOT**
(the CI results, as of the date shown). Nothing simulated looks live.

### Suggested order for a 15-minute talk

| min | scene | what to do |
|---|---|---|
| 0–2 | 1 Architecture | The problem: one WMS, many kinds of resource. Click `submit()` and `Diagnosable` to show the essential and the optional capabilities, straight from the docstrings. |
| 2–3 | 2 Composition | N transports + M schedulers, not N × M classes. Green cells run in CI. |
| 3–5 | 3 Translation | Edit the spec: the same one becomes a submit description and a job array. Click *In a container*, *URL input*, *Outputs by pattern*: each backend refuses what it cannot honour, before anything runs. |
| 5–9 | 4 Job board | Submit the quick job to all four. Same consumer code, four dialects. Submit *30 s with progress*; on the Fake, select one and press Diagnostics; on a real backend, the same button says the backend is not `Diagnosable`. Get output, then kill. |
| 9–10 | 5 Trace | *Compare the latest submit on two backends*: the top is the same, the bottom is completely different. Click an arrow to show the argv, the staged files, the token request. |
| 10–12 | 6 Lifecycle | Show the handle going into the database. *Restart interCEde*: a new backend finds every job from the stored handles alone. *Forged handle*: UNKNOWN, never dropped. |
| 12–14 | 7 Partial failure | *Some copies refused*, then *Whole resource down* (raises) against *Per-job failures* (does not), then *Kill running and finished jobs* on a real backend. |
| 14–15 | 8 Verified | Every stack × version runs the same contract suite on every pull request. |

If Docker or the network lets you down, switch to **Replay**: `demo/recordings/` holds a complete
run of scenes 4–7 against the live stacks.

## Recording and replay

**Record** saves every event the browser sees, from a full snapshot on, until you press it again,
into `demo/recordings/<date-time>.json.gz`. **Replay** plays one back at 0.5–8×, through the same
views, with the badge forced to REPLAY. While a recording plays, live events reach no browser and
every action that would reach a backend is refused (HTTP 409).

Left out of recordings: the background `get_status` polls, which are most of the events and whose
findings the board's own updates already carry. Collected output files are not in a recording
either; replayed, their links only open what is still on this machine.

## The CI snapshot

Scene 8 shows `demo/data/ci.json`, which the app never refreshes itself. Before the talk, with the
network:

```sh
pixi run -e demo python demo/scripts/fetch_ci.py   # GITHUB_TOKEN only raises the rate limit
```

It reads the last runs of `integration.yml` on DIRACGrid/intercede and Alexander-Auer/intercede
from the GitHub REST API, and the jobs CI names `stack / config / version` from `stacks.yml`.

## How it is instrumented

Nothing under `src/` is changed or reimplemented. Every status, handle, command line and outcome
shown in Live comes from a real call into interCEde. The demo wraps the real objects by
composition, at these seams (`demo/tracing.py`, `demo/columns.py`):

- **The backend**, as the consumer sees it. Since Python 3.12, `isinstance` on a
  `runtime_checkable` protocol looks attributes up statically, so a delegating `__getattr__`
  would satisfy no protocol at all. `traced_backend` therefore builds the wrapper class per
  backend, with exactly the operations the backend has: a wrapped `BatchBackend` is no more
  `Diagnosable` than the real one, and the board's capability chips are those `isinstance` checks.
- **Batch:** the registry's own classes (`registry._load`), composed the way `registry._build`
  composes them: `BatchBackend.from_settings(settings, transport=TracingTransport(…),
  scheduler=TracingScheduler(…))`, after the same `registry._checked` protocol checks. The traced
  transport records every argv and every file `put` stages; the traced scheduler records each
  command it builds and each parse.
- **HTCondor-CE:** the registry's class, built with `runner=TracingRunner(run_process)`. The
  runner is the one callable every `condor_*` tool runs through, so the trace has each tool's
  argv, the submit description, and the output. Of the environment it receives (all of
  `os.environ`), only `CONDOR_CONFIG` and `_CONDOR_*` are recorded.
- **The credential provider:** `TracingProvider` around `StaticCredentials`, holding what the
  stack minted, read with the integration suite's own readers (`tests/integration/conftest.py`).
  It shows when a backend asks, for what kind and with which scopes.
- **local + Slurm** must run where Slurm is, as the integration suite does (`exec_in_container`).
  `demo/agent.py` is started with `docker compose exec` in the Slurm container, runs a real
  `BatchBackend` (local transport, Slurm) built and traced the same way, and passes its trace
  steps back over stdin/stdout. On this host `AgentBackend` forwards the consumer's calls. The
  payload script and the collected files cross that pipe too, since the container has neither.
- **Scene 3 (DRY RUN):** no pure function in interCEde gives an HTCondor submit description; the
  backend's `submit()` assembles it. So the real `submit()` of each backend runs against a recorder
  at the lowest seam (a runner that reads the description, a transport that keeps what `put`
  stages) and is answered with made-up job IDs that are thrown away (`demo/dryrun.py`).

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
recording round-trips and replays behind the gate, and that no credential or per-copy secret
reaches a recording. The library's own suite does not collect them (`testpaths = ["tests"]`).

## What the demo shows about the library

- Only the Fake implements `Diagnosable`; `BatchBackend` and `HTCondorCEBackend` do not, and the
  board shows that instead of calling them.
- "SSH via a jump host" is the `jump_host` field of `BatchSSHConfig`, not a transport of its own.
  No CI stack uses it yet, so scene 2 marks it implemented, not verified.
- HTCondor-CE selects outputs by name and refuses a pattern, so the shared payload names its
  output files.
- `arc-slurm` is listed in `stacks.yml` without a stack folder; scenes 2 and 8 say so.

## Layout

| path | what |
|---|---|
| `app.py`, `__main__.py` | the server: one event stream (SSE), the presenter's actions |
| `events.py` | the bus, and the redaction on it |
| `tracing.py` | the tracing wrappers |
| `columns.py`, `agent.py` | the backends on the board, built from the stacks' configuration |
| `consumer.py` | the consumer code shown on the board, the same for every backend |
| `board.py`, `payloads.py`, `scenarios.py` | the board, its payloads, the scripted scenes |
| `views.py`, `dryrun.py` | the views read from the code, and the dry run |
| `recording.py`, `recordings/` | record and replay, and the committed live run |
| `scripts/fetch_ci.py`, `data/ci.json` | the CI snapshot |
| `static/` | the browser side: plain ES modules, SVG and CSS, no build step |
