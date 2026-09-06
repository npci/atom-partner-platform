# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared request shape for inbound A2A task handlers.

`TaskReceiveRequest` is the legacy re-pack of an inbound A2A message that the
executor constructs and passes down to each per-task-type handler. Kept in its
own module so handler modules and `__init__` can both import it without a cycle.
"""
from __future__ import annotations

from pydantic import BaseModel


class TaskReceiveRequest(BaseModel):
    """Re-pack of the incoming A2A message into the legacy shape so the handler
    bodies didn't have to be rewritten when the SDK wire was introduced. The
    executor constructs one of these from the SDK Message and passes it down."""
    task_type: str
    change_id: str | None = None
    payload: dict | None = None
    task_id: str | None = None
    # Inbound envelope message_id — the spec's `in_response_to` target for any
    # reply (e.g. proposal_acknowledged threads to the change_communication).
    message_id: str | None = None
    # Inbound envelope correlation_id — NPCI's per-(change, bank) thread id.
    # Captured on receipt and echoed on every reply about this change (v1.1 §5).
    correlation_id: str | None = None
