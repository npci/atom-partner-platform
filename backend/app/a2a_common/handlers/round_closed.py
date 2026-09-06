# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `round_closed`.

NPCI→bank per-partner notice that a negotiation round has closed. Reason
covers pm_forced, silent_acceptance, superseded_by_version, and frozen (cap
reached). Appends to the `round_notices` JSON log — when a next round opens
the partner gets a separate `round_opened` notice with its number and
deadline, so no next-round hint is duplicated here.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_round_closed(body: TaskReceiveRequest, db: Session) -> dict:
    payload = body.payload or {}
    npci_change_id = payload.get("change_id", body.change_id or "")
    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == npci_change_id)
        .first()
    )
    if not change:
        return {"status": "accepted", "message": "round_closed for unknown change — ignored"}

    # protobuf Struct encodes numbers as doubles on the wire — coerce back.
    def _as_int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return v
    round_number = _as_int(payload.get("round_number"))
    close_reason = payload.get("close_reason") or "closed"
    # Human message the ChangeDetail timeline renders in the notice bubble.
    # The pre-existing round_closed renderer reads `message`, so build one
    # from the structured close_reason for consistent display across the
    # (formerly piggybacked) and new first-class notices.
    _reason_text = {
        "pm_forced":              "NPCI's PM has closed this negotiation round.",
        "silent_acceptance":      "The negotiation round timed out (no response within the round window).",
        "superseded_by_version":  "A new kit version has been shipped; the current round is closed.",
        "frozen":                 "Negotiation has frozen — no further rounds will open for this change.",
    }.get(close_reason, f"Negotiation round closed ({close_reason}).")

    entry = {
        "event":              "round_closed",
        "round_number":       round_number,
        # Alias so the ChangeDetail timeline (reads negotiation_round) renders
        # the "Round N closed" badge without any frontend change.
        "negotiation_round":  round_number,
        "closed_at":          payload.get("closed_at"),
        "close_reason":       close_reason,
        "message":            _reason_text,
        "received_at":        datetime.now(timezone.utc).isoformat(),
    }
    try:
        log = json.loads(change.round_notices) if change.round_notices else []
    except (ValueError, TypeError):
        log = []
    if not isinstance(log, list):
        log = []
    log.append(entry)
    change.round_notices = json.dumps(log)
    # close_reason=="frozen" mirrors the terminal signal. Set the local
    # finalized flag so the composer locks even if the separate
    # negotiation_frozen notice is delayed or missed (idempotent — the
    # frozen handler sets the same field).
    if entry["close_reason"] == "frozen" and not change.negotiation_finalized_at:
        change.negotiation_finalized_at = entry["closed_at"] or entry["received_at"]
    db.commit()
    logger.info(
        "round_closed recorded: local_id=%s round=%s reason=%s",
        change.id, entry["round_number"], entry["close_reason"],
    )
    return {
        "status":   "accepted",
        "message":  "round_closed recorded",
        "local_id": change.id,
    }
