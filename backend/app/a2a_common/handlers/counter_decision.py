# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `counter_decision` — NPCI's decision (ACCEPT / REJECT)
on a partner-submitted counter proposal (protocol v1 §6.7).

Pre-v1 this rode inside `clarification_response` with
`message_kind=COUNTER_DECISION`; it is now a first-class task type. Appends to
`IncomingChange.counter_decisions` and mirrors the verdict onto the partner's
originating negotiation-kind OutgoingQuery."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange, OutgoingQuery

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_counter_decision(body: TaskReceiveRequest, db: Session) -> dict:
    payload = body.payload or {}
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    change_id = inner.get("change_id") or payload.get("change_id") or body.change_id

    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == change_id)
        .first()
    )
    if not change:
        return {"status": "accepted", "message": "Counter decision (no matching change)"}

    decision = inner.get("decision", "")
    resolution_text = inner.get("resolution_text") or inner.get("response") or f"Decision: {decision}"
    existing = []
    if change.counter_decisions:
        try:
            existing = json.loads(change.counter_decisions) or []
        except (json.JSONDecodeError, TypeError):
            existing = []
    existing.append({
        "decision":          decision,                # 'ACCEPT' | 'REJECT'
        "in_response_to":    inner.get("in_response_to"),
        "negotiation_round": inner.get("negotiation_round"),
        "response_text":     resolution_text,
        "original_text":     inner.get("original_justification") or "",
        "received_at":       datetime.now(timezone.utc).isoformat(),
    })
    change.counter_decisions = json.dumps(existing)

    # Mirror the decision onto the partner's outgoing negotiation query so the
    # messaging section shows accepted/rejected instead of staying "sent".
    now = datetime.now(timezone.utc)
    pending_query = (
        db.query(OutgoingQuery)
        .filter(
            OutgoingQuery.change_id == change.id,
            OutgoingQuery.kind == "negotiation",
            OutgoingQuery.status == "sent",
        )
        .order_by(OutgoingQuery.sent_at.desc())
        .first()
    )
    if pending_query:
        pending_query.status = "accepted" if decision == "ACCEPT" else "rejected"
        pending_query.response = resolution_text
        pending_query.response_received_at = now

    db.commit()
    logger.info(
        "Counter decision received: change=%s decision=%s round=%s",
        change.id, decision, inner.get("negotiation_round"),
    )
    return {"status": "accepted", "message": "Counter decision recorded"}
