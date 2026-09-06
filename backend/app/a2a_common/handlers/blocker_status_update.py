# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `blocker_status_update` — NPCI's interim, non-terminal
status push on a partner-reported blocker (protocol v1 §6.13).

Unlike `blocker_resolution` this does NOT close the blocker. It records the
latest investigation status (triaged / in_investigation / fix_in_progress / …)
plus optional CRM ref + ETA, and appends to a per-blocker `status_history` on
the matching `IncomingChange.blockers` entry."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_blocker_status_update(body: TaskReceiveRequest, db: Session) -> dict:
    payload = body.payload or {}
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    change_id = inner.get("change_id") or payload.get("change_id") or body.change_id

    target_blocker_id = inner.get("in_response_to_blocker") or inner.get("blocker_id")
    if not target_blocker_id:
        logger.warning("BLOCKER_STATUS_UPDATE missing in_response_to_blocker")
        return {"status": "accepted", "message": "Blocker status update (no target id)"}

    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == change_id)
        .first()
    )
    if not change:
        logger.warning("BLOCKER_STATUS_UPDATE for unknown change %s", change_id)
        return {"status": "accepted", "message": "Blocker status update (no matching change)"}

    blockers = []
    if change.blockers:
        try:
            blockers = json.loads(change.blockers) or []
        except (json.JSONDecodeError, TypeError):
            blockers = []

    entry = {
        "status":                  inner.get("status"),
        "assigned_team":           inner.get("assigned_team"),
        "estimated_resolution_by": inner.get("estimated_resolution_by"),
        "crm":                     inner.get("crm"),
        "notes":                   inner.get("notes"),
        "received_at":             datetime.now(timezone.utc).isoformat(),
    }

    matched = False
    for b in blockers:
        if b.get("blocker_id") == target_blocker_id:
            # Non-terminal: update the live status, keep history.
            b["status"] = entry["status"] or b.get("status")
            b["assigned_team"] = entry["assigned_team"]
            b["estimated_resolution_by"] = entry["estimated_resolution_by"]
            b["crm"] = entry["crm"]
            b.setdefault("status_history", []).append(entry)
            matched = True
            break
    if not matched:
        blockers.append({
            "blocker_id":     target_blocker_id,
            "status":         entry["status"],
            "description":    "(prior to local persistence)",
            "status_history": [entry],
        })

    change.blockers = json.dumps(blockers)
    db.commit()
    logger.info(
        "Blocker status update received: change=%s blocker=%s status=%s",
        change.id, target_blocker_id, entry["status"],
    )
    return {"status": "accepted", "message": "Blocker status update recorded"}
