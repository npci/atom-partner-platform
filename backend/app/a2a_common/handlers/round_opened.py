# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `round_opened`.

NPCI→bank per-partner notice that a negotiation round has opened. The partner
has no round-state table of its own — this notice is appended to the existing
`round_notices` JSON log so the change timeline surfaces which round is
currently active, its deadline, and why it opened (initial ack / PM force-
advance / silent advance / new version ship).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_round_opened(body: TaskReceiveRequest, db: Session) -> dict:
    payload = body.payload or {}
    npci_change_id = payload.get("change_id", body.change_id or "")
    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == npci_change_id)
        .first()
    )
    if not change:
        return {"status": "accepted", "message": "round_opened for unknown change — ignored"}

    # protobuf Struct on the wire encodes numbers as doubles, so ints arrive
    # here as floats ("Round 1.0 of 2.0" instead of "Round 1 of 2"). Coerce
    # back to int for the human-readable message and the stored entry.
    def _as_int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return v
    round_number = _as_int(payload.get("round_number"))
    max_rounds = _as_int(payload.get("max_rounds"))
    opened_reason = payload.get("opened_reason") or "opened"
    deadline_at = payload.get("deadline_at")
    _reason_text = {
        "initial_ack":            f"Round {round_number} of {max_rounds or '?'} is now open. Respond by {deadline_at or 'the round deadline'}.",
        "version_ship":           f"A new kit version has shipped — round {round_number} of {max_rounds or '?'} is now open. Respond by {deadline_at or 'the round deadline'}.",
        "pm_advance_no_change":   f"NPCI's PM has opened round {round_number} of {max_rounds or '?'}. Respond by {deadline_at or 'the round deadline'}.",
        "silent_advance":         f"Round {round_number} of {max_rounds or '?'} is now open (auto-advanced). Respond by {deadline_at or 'the round deadline'}.",
    }.get(opened_reason, f"Round {round_number} of {max_rounds or '?'} is now open.")

    entry = {
        "event":              "round_opened",
        "round_number":       round_number,
        # Alias so the ChangeDetail timeline (reads negotiation_round) can
        # render a "Round N opened" badge alongside the existing closed one.
        "negotiation_round":  round_number,
        "max_rounds":         max_rounds,
        "deadline_at":        deadline_at,
        "kit_version":        _as_int(payload.get("kit_version")),
        "opened_reason":      opened_reason,
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
    db.commit()
    logger.info(
        "round_opened recorded: local_id=%s round=%s reason=%s",
        change.id, entry["round_number"], entry["opened_reason"],
    )
    return {
        "status":   "accepted",
        "message":  "round_opened recorded",
        "local_id": change.id,
    }
