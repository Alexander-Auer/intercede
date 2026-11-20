"""What every backend does the same way, written once.

Only what means exactly the same on every backend belongs here. What one
backend does its own way stays with that backend, even when it looks alike:
these are plain functions and values, never a base class.
"""

from __future__ import annotations

from intercede.models import JobStatus

# A job in one of these can still be cancelled; in any other it has finished,
# or the backend no longer knows it.
ACTIVE = frozenset({JobStatus.SUBMITTED, JobStatus.WAITING, JobStatus.RUNNING})
