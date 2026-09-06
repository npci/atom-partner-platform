# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-job correlation id propagation (Finding 13:
security_architecture_skills.md §13.1 — correlation IDs MUST propagate across
sync calls, async messages, internal modules, and dependency calls).

`api/dashboard/jobs.py::_run_job` sets the active correlation id (the
triggering `AgentJob.correlation_id`) for the duration of the runner call;
`npci_client.send_task()` reads it as the default `correlation_id=` when the
caller doesn't pass one explicitly. This lets the ~10 existing send_task()
call sites across `api/dashboard/*.py` pick up correlation propagation for
free, without a signature change at every call site — only the ones that
want a MORE specific id (e.g. an OutgoingQuery's own correlation_id, which
already existed before this change and takes precedence) need to pass one.

A contextvar, not a thread-local: FastAPI's BackgroundTasks + this platform's
job runners execute synchronously within a single call stack per job, so a
plain contextvars.ContextVar correctly isolates one job's correlation id from
another's without any extra plumbing.
"""
from __future__ import annotations

import contextlib
import contextvars

_CURRENT_CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_CURRENT_CORRELATION_ID", default=None,
)


def current_correlation_id() -> str | None:
    """The active job's correlation id, or None outside any job context
    (e.g. a request-handler-initiated send_task() call with no AgentJob)."""
    return _CURRENT_CORRELATION_ID.get()


@contextlib.contextmanager
def use_correlation_id(correlation_id: str | None):
    """Context manager: sets the active correlation id for the duration of
    the block. Used by jobs.py::_run_job to scope a job's correlation id to
    its runner call."""
    token = _CURRENT_CORRELATION_ID.set(correlation_id)
    try:
        yield
    finally:
        _CURRENT_CORRELATION_ID.reset(token)
