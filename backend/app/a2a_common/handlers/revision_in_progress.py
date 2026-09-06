# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `revision_in_progress`.

NPCI→bank advisory sent when a negotiation round closes and a revised kit is
being prepared. Sets the hold flag so the partner UI freezes the query box; the
flag clears when the new kit ships (see `change_communication`).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_revision_in_progress(body: TaskReceiveRequest, db: Session) -> dict:
    payload = body.payload or {}
    npci_change_id = payload.get("change_id", body.change_id or "")
    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == npci_change_id)
        .first()
    )
    if not change:
        return {"status": "accepted", "message": "Revision advisory for unknown change — ignored"}

    in_progress = bool(payload.get("in_progress", True))
    change.revision_in_progress = in_progress
    change.revision_target_version = payload.get("target_version") if in_progress else None
    db.commit()
    logger.info(
        "Revision-hold %s: local_id=%s target=%s",
        "set" if in_progress else "cleared", change.id, change.revision_target_version,
    )
    return {"status": "accepted", "message": "Revision advisory recorded", "local_id": change.id}
