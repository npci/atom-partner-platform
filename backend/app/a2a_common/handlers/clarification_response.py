# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `clarification_response`.

Sub-modes discriminated by `payload.message_kind` (protocol-v1 extensions):
  - COUNTER_PROPOSAL  — NPCI's counter-back in a multi-round negotiation (ext)
  - ROUND_CLOSED      — NPCI force-closed a negotiation round (ext)
  - (absent/other)    — a regular Q&A clarification attached to an OutgoingQuery

NPCI's ACCEPT/REJECT verdict on a partner counter is no longer carried here —
it's the first-class `counter_decision` task type (see `counter_decision.py`).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange, OutgoingQuery

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_clarification_response(body: TaskReceiveRequest, db: Session) -> dict:
    """Process incoming clarification — either a regular Q&A response or
    NPCI's counter-back in a multi-round negotiation. Discriminator is
    `payload.message_kind`: 'COUNTER_PROPOSAL' means counter, anything
    else (or absent) is a regular clarification."""
    payload = body.payload or {}
    # Wire shape post-Slice-8: full A2A wrapper with {task_type, payload, …}.
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    change_id = inner.get("change_id") or payload.get("change_id") or body.change_id

    # ── NPCI counter-back ────────────────────────────────────────────
    # NPCI's counter is delivered as a clarification_response carrying
    # message_kind=COUNTER_PROPOSAL. Store on the change row so the UI
    # can render a structured Accept/Counter-back card.
    if inner.get("message_kind") == "COUNTER_PROPOSAL":
        change = (
            db.query(IncomingChange)
            .filter(IncomingChange.npci_change_id == change_id)
            .first()
        )
        if change:
            counter_payload = {
                "counter_proposal_id": inner.get("counter_proposal_id"),
                "negotiation_round":   inner.get("negotiation_round"),
                "justification":       inner.get("justification") or inner.get("response"),
                "valid_until":         inner.get("valid_until"),
                "received_at":         datetime.now(timezone.utc).isoformat(),
                "status":              "open",
            }
            change.npci_counter = json.dumps(counter_payload)
            # Surface in the decision panel — back to negotiating from
            # whatever the prior state was.
            change.decision = "negotiating"
            db.commit()
            logger.info(
                "NPCI counter received: change=%s round=%s",
                change.id, counter_payload.get("negotiation_round"),
            )
            return {"status": "accepted", "message": "Counter-proposal received"}
        logger.warning("NPCI counter for unknown change %s", change_id)
        return {"status": "accepted", "message": "Counter-proposal received (no matching change)"}

    # ── NPCI round-closed notice ─────────────────────────────────────
    # PM force-closed a negotiation round. The partner has no round UI,
    # so append to incoming_changes.round_notices for the timeline.
    if inner.get("message_kind") == "ROUND_CLOSED":
        change = (
            db.query(IncomingChange)
            .filter(IncomingChange.npci_change_id == change_id)
            .first()
        )
        if change:
            notices = []
            if change.round_notices:
                try:
                    notices = json.loads(change.round_notices) or []
                except (json.JSONDecodeError, TypeError):
                    notices = []
            notices.append({
                "negotiation_round": inner.get("negotiation_round"),
                "message":           inner.get("response") or "NPCI closed this negotiation round.",
                "closed_at":         inner.get("closed_at"),
                "received_at":       datetime.now(timezone.utc).isoformat(),
            })
            change.round_notices = json.dumps(notices)
            db.commit()
            logger.info(
                "Round-closed notice received: change=%s round=%s",
                change.id, inner.get("negotiation_round"),
            )
            return {"status": "accepted", "message": "Round-closed notice recorded"}
        logger.warning("Round-closed notice for unknown change %s", change_id)
        return {"status": "accepted", "message": "Round-closed notice (no matching change)"}

    # ── BRD mandatory-requirement violation auto-rejection ───────────────
    if inner.get("message_kind") == "BRD_VIOLATION":
        req_label = inner.get("requirement", "")
        reason = inner.get("reason", "")
        corr_id_v = inner.get("correlation_id") or payload.get("correlation_id")
        change_ids_sub_v = db.query(IncomingChange.id).filter(IncomingChange.npci_change_id == change_id)
        query_v = None
        if corr_id_v:
            query_v = (
                db.query(OutgoingQuery)
                .filter(
                    OutgoingQuery.change_id.in_(change_ids_sub_v),
                    OutgoingQuery.correlation_id == corr_id_v,
                )
                .first()
            )
        if query_v is None:
            query_v = (
                db.query(OutgoingQuery)
                .filter(
                    OutgoingQuery.change_id.in_(change_ids_sub_v),
                    OutgoingQuery.kind == "general",
                    OutgoingQuery.status == "sent",
                )
                .order_by(OutgoingQuery.sent_at.desc())
                .first()
            )
        if query_v:
            query_v.status = "auto_rejected"
            query_v.response = json.dumps({
                "type": "brd_rejection",
                "requirement": req_label,
                "reason": reason,
            })
            query_v.response_received_at = datetime.now(timezone.utc)
            db.commit()
            logger.info(
                "BRD violation rejection recorded: query=%s change=%s req=%s",
                query_v.id, change_id, req_label,
            )
        else:
            logger.warning("BRD violation: no matching query for change=%s corr=%s", change_id, corr_id_v)
        return {"status": "accepted", "message": "BRD violation rejection recorded"}

    # ── Regular clarification — attach to the originating OutgoingQuery.
    # Preferred: match by `correlation_id` echoed by NPCI from the
    # original query envelope. This binds the response to the EXACT row
    # the partner sent, regardless of how many other queries are open
    # in the same channel.
    # Legacy fallback: most-recent-pending-in-channel — only fires for
    # responses to queries from older partner builds that didn't yet
    # send a correlation_id. Logged so we can detect when this path is
    # still in use post-rollout.
    response_text = inner.get("response", "") or payload.get("response", "")
    channel = inner.get("channel") or payload.get("channel") or "general"
    if channel not in ("general", "cert"):
        channel = "general"
    # v1.1: prefer the spec's `query_id`; fall back to `correlation_id` for
    # older NPCI builds. Both carry the same OutgoingQuery row id.
    correlation_id = (
        inner.get("query_id")
        or inner.get("correlation_id")
        or payload.get("query_id")
        or payload.get("correlation_id")
    )

    change_ids_subq = (
        db.query(IncomingChange.id)
          .filter(IncomingChange.npci_change_id == change_id)
    )

    query = None
    if correlation_id:
        query = (
            db.query(OutgoingQuery)
            .filter(
                OutgoingQuery.change_id.in_(change_ids_subq),
                OutgoingQuery.correlation_id == correlation_id,
            )
            .first()
        )

    if query is None:
        query = (
            db.query(OutgoingQuery)
            .filter(
                OutgoingQuery.change_id.in_(change_ids_subq),
                OutgoingQuery.kind == channel,
                OutgoingQuery.status == "sent",
                OutgoingQuery.correlation_id.is_(None),
            )
            .order_by(OutgoingQuery.sent_at.desc())
            .first()
        )
        if query is not None:
            logger.warning(
                "Clarification response matched by legacy 'most recent' fallback "
                "(no correlation_id echoed) — change=%s channel=%s query=%s",
                change_id, channel, query.id,
            )

    if query:
        now = datetime.now(timezone.utc)
        if channel == "general" and query.response:
            # Follow-up reply to an already-answered general question.
            # Don't clobber the first answer — append to the change's
            # npci_followups log so the timeline shows every NPCI reply,
            # the way the NPCI side keeps each PO_APPROVED message. (Cert
            # channel keeps the single-response shape; it has its own
            # inbox that reads OutgoingQuery.response directly.)
            change = db.get(IncomingChange, query.change_id)
            followups = []
            if change and change.npci_followups:
                try:
                    followups = json.loads(change.npci_followups) or []
                except (json.JSONDecodeError, TypeError):
                    followups = []
            followups.append({
                "query_id":    query.id,
                "message":     response_text,
                "received_at": now.isoformat(),
            })
            if change:
                change.npci_followups = json.dumps(followups)
        else:
            query.response = response_text
            query.status = "answered"
            query.response_received_at = now
        db.commit()
        logger.info(
            "Clarification response received for query=%s channel=%s correlation_id=%s",
            query.id, channel, correlation_id,
        )
    else:
        logger.warning(
            "No matching %s-channel query found for change=%s correlation_id=%s",
            channel, change_id, correlation_id,
        )

    return {"status": "accepted", "message": "Clarification response received", "channel": channel}
