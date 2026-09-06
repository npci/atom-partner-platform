# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `negotiation_frozen`.

NPCI→bank notice that the negotiation has frozen (the round cap was reached).
Sets negotiation_finalized_at so the partner UI locks the decision/composer —
needed because a round-based freeze doesn't bump the kit version, so the
partner can't infer the freeze from a newly-shipped kit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_negotiation_frozen(body: TaskReceiveRequest, db: Session) -> dict:
    payload = body.payload or {}
    npci_change_id = payload.get("change_id", body.change_id or "")
    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == npci_change_id)
        .first()
    )
    if not change:
        return {"status": "accepted", "message": "Freeze notice for unknown change — ignored"}

    change.negotiation_finalized_at = (
        payload.get("negotiation_finalized_at")
        or datetime.now(timezone.utc).isoformat()
    )
    # Freeze ends negotiation — there is no next kit version, so any pending
    # revision-hold is now moot. Clear it; a leftover flag would otherwise block
    # the partner's clarifying-query composer + endpoint on a frozen change.
    change.revision_in_progress = False
    change.revision_target_version = None
    db.commit()
    logger.info(
        "Negotiation frozen: local_id=%s finalized_at=%s",
        change.id, change.negotiation_finalized_at,
    )
    return {"status": "accepted", "message": "Freeze recorded", "local_id": change.id}
