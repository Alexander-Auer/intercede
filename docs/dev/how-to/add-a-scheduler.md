# Add a scheduler

A scheduler is how work is queued on a resource. It works over every transport as soon as it exists:
you write no code per combination. See [Composed backends](../explanations/composed-backends.md) for
why, and the `Scheduler` protocol in `src/intercede/_backends/batch/protocols.py` for the details of
each method.

## Steps

1. **Put what the batch system does in a core module**, `src/intercede/_backends/_<name>/`: its job
   description, its tools' argv, what they print, and the map from its states to `JobStatus`. Nothing
   in it runs anything, so it is tested with output copied from the real tools. `_slurm/` is the
   example.
2. **Add the scheduler class** in `src/intercede/_backends/batch/scheduler/<name>.py`. It only calls
   the core module, and has a `from_settings(settings)` classmethod.
3. **Queue the runner once per copy.** The job must run `sh run.sh <copy>` from the submission's
   directory, and write the scheduler's log to `layout.scheduler_log(<copy>)`. Environment, inputs and
   secrets are already taken care of. Return the files you need (a job description, say) from
   `submit_command`, and the backend stages them.
4. **Register it** in `_SCHEDULERS` in `src/intercede/registry.py`, under the name
   `_BatchConfig.scheduler` lists for it in `src/intercede/config.py`.
5. **Add its stacks.** One `compose` environment, and an entry in `stacks.yml` per transport. See
   [Add an integration stack](add-an-integration-stack.md).

## What to get right

- **Tell "no such job" from a failure.** Many tools exit non-zero for a job they have forgotten. That
  is an answer, so the parser returns no status for it. A parser raises `ValueError` only for a
  command that really failed, because that fails the whole operation.
- **Ask two places if the tool does.** `status_command` asks the queue, and `history_command` asks
  about the jobs the queue did not know. A batch system with one place returns nothing from the second.
- **Be strict in `is_job_id`.** IDs come back from stored handles and end up on command lines.

## What you do not write

The shared tests already cover a new scheduler. `test_composition.py` picks it up from
`registry.available()`, and the `contract` tests run on its stacks. A test of your own is only for
what your batch system alone does, as in `tests/integration/test_slurm.py`.
