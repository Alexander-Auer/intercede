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

No test checks this, since it is how the parts are built, not what they do: keep it in review. The
shared suite runs every combination that ships, on its stacks.

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

## How a job's files reach the node that runs it

The backend stages a submission on the host the transport reaches, and collects from there. Between
that host and the node that runs a job, a scheduler takes one of two ways, which
`Scheduler.stages_own_files` says:

- **The job runs where its files are** (`False`, Slurm). The nodes must share that host's file
  system, as a Slurm cluster's do.
- **The job moves its files itself** (`True`, HTCondor). HTCondor copies what every copy shares
  (`layout.shared`) and the job's own copy's directory to the node, and brings that directory back
  when the job ends, at the same paths. The nodes need no shared file system, and a job never gets
  another copy's directory, so not its secrets.

Either way the backend does the same, so it never asks which way it is: what a job leaves is in its
copy's directory on that host when the job has ended.
