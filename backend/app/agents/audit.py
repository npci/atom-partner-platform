# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agent-run audit helpers — write and finish `agent_runs` rows.

Called only by `app.agents.base.Agent.execute()`, which wraps every agent
invocation (in-process or remote). Keeps the audit logic in one place so all
bindings produce identical rows. Partner-side mirror of the NPCI backend's
`job_registry` create/complete/fail pattern over `agent_jobs`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AgentRun

logger = logging.getLogger(__name__)

_MAX_SCALAR = 200


def _summarise_input(data: object) -> dict:
    """Bounded, JSON-safe summary of the agent input — never the full payload.

    Strings are truncated; collections become a type+size marker. This keeps
    the audit table light even when an agent is handed a large change blob.
    """
    if not isinstance(data, dict):
        return {"_type": type(data).__name__}
    out: dict = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = v[:_MAX_SCALAR] + ("…" if len(v) > _MAX_SCALAR else "")
        elif isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = f"<{type(v).__name__} len={len(v)}>"
        elif isinstance(v, dict):
            out[k] = f"<dict keys={list(v.keys())[:10]}>"
        else:
            out[k] = f"<{type(v).__name__}>"
    return out


def audit_start(
    db: Session,
    *,
    agent_name: str,
    mode: str = "local",
    endpoint: str | None = None,
    input: object = None,
    change_id: str | None = None,
    user_id: str | None = None,
) -> AgentRun:
    """Open an `agent_runs` row in `status='running'` and return it."""
    row = AgentRun(
        agent_name=agent_name,
        mode=mode,
        endpoint=endpoint,
        status="running",
        change_id=change_id,
        input_summary=_summarise_input(input),
        meta=({"user_id": user_id} if user_id else {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _latency_ms(row: AgentRun) -> int:
    start = row.started_at
    if start.tzinfo is None:  # Postgres returns naive UTC for DateTime cols
        start = start.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - start).total_seconds() * 1000)


def audit_succeed(
    db: Session, row: AgentRun, output: object = None, *, http_status: int | None = None
) -> None:
    row.status = "succeeded"
    row.completed_at = datetime.now(timezone.utc)
    row.latency_ms = _latency_ms(row)
    if http_status is not None:
        row.http_status = http_status
    if isinstance(output, dict):
        row.result_payload = output
    db.commit()


def audit_fail(
    db: Session, row: AgentRun, error: object, *, http_status: int | None = None
) -> None:
    row.status = "failed"
    row.completed_at = datetime.now(timezone.utc)
    row.latency_ms = _latency_ms(row)
    if http_status is not None:
        row.http_status = http_status
    row.error_message = str(error)[:4096]
    db.commit()
