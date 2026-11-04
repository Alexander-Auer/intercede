# Composed backends

Reaching a resource and queueing work on it are independent choices. So interCEde has one backend,
`BatchBackend`, made of one `Transport` (local, SSH) and one `Scheduler` (Slurm, ...). That makes N
transports and M schedulers N + M classes instead of N x M, and a new combination needs no code.

## What each part knows

| Part | Knows | Never knows |
| --- | --- | --- |
| `Transport` | how to run an argv and copy files on the resource | which batch system is there |
| `Scheduler` | its tools' command lines and output | how the resource is reached, a copy's environment and secrets |
| `BatchBackend` | which files a job needs, and how to find them again | which transport or scheduler it has |

Tests check this: `tests/unit/backends/batch/test_composition.py` reads the imports of each part,
and runs every combination that ships. It also runs a second scheduler that exists only in the
tests, so that a backend shaped around Slurm would fail.

## No Python on the resource

DIRAC ships a Python driver to the remote host and runs it there. interCEde builds every command on
the consumer's host and sends only argv and files. So the resource needs a POSIX login shell and
its batch system's tools, and nothing of Python or interCEde.

## How a copy starts

The backend stages a small shell script, `run.sh`, with the submission. It takes the copy's index,
goes to that copy's directory, sets its environment and starts the program. A scheduler only queues
that script once per copy (`layout.py` describes the files). So what differs per copy is handled once
for all schedulers, and a copy's environment and secrets never reach a job description.

## Why there is no `Stager`

The ADR asks whether moving a job's files should be a third collaborator next to `Transport` and
`Scheduler`. It should not. Staging only uses `Transport.put` and `Transport.get`, and it works the
same for every scheduler, so a `Stager` would have one implementation and would never vary. It would
be an axis in name only. The backend does the staging itself.

`Scheduler.stages_own_files` stays in the protocol for a scheduler that moves files itself. None does
yet, so the backend refuses such a scheduler instead of half supporting it.
