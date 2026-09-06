# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `blocker_resolution` — NPCI's resolution of a
partner-reported blocker. Patches the matching blocker entry on
`IncomingChange.blockers` (JSON list) with the resolution."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_blocker_resolution(body: TaskReceiveRequest, db: Session) -> dict:
    """Process NPCI's resolution of a partner-reported blocker.

    Wire format: task_type=blocker_resolution, payload contains
    in_response_to_blocker (matching the partner's blocker_id),
    resolution.{action_taken, artifact_ref, resolved_at}, and
    resolution_text. Partner-side persistence is on
    IncomingChange.blockers (JSON list); this handler finds the
    matching blocker by blocker_id and patches in the resolution.
    """
    payload = body.payload or {}
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    change_id = inner.get("change_id") or payload.get("change_id") or body.change_id

    target_blocker_id = (
        inner.get("in_response_to_blocker")
        or inner.get("blocker_id")
    )
    if not target_blocker_id:
        logger.warning("BLOCKER_RESOLUTION missing in_response_to_blocker")
        return {"status": "accepted", "message": "Blocker resolution received (no target id)"}

    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == change_id)
        .first()
    )
    if not change:
        logger.warning("BLOCKER_RESOLUTION for unknown change %s", change_id)
        return {"status": "accepted", "message": "Blocker resolution (no matching change)"}

    blockers = []
    if change.blockers:
        try:
            blockers = json.loads(change.blockers) or []
        except (json.JSONDecodeError, TypeError):
            blockers = []

    # Patch the matching blocker entry. If we don't find one (e.g.
    # change persistence was added after some blockers were reported),
    # append a stub so the resolution is still visible to the partner.
    received_at = datetime.now(timezone.utc).isoformat()

    # `resolution` is either the legacy DETAILS object or the v1.1 spec STRING
    # enum (resolved/wontfix/deferred) with details at top-level. Tolerate both.
    _res = inner.get("resolution")
    if isinstance(_res, dict):
        disposition   = "resolved"
        action_taken  = _res.get("action_taken")
        artifact_ref  = _res.get("artifact_ref")
        resolved_at   = _res.get("resolved_at")
    else:
        disposition   = _res or "resolved"
        action_taken  = inner.get("action_taken")
        _patched      = inner.get("patched_artefacts") or []
        artifact_ref  = (_patched[0] if _patched else None) or inner.get("artifact_ref")
        resolved_at   = inner.get("resolved_at")

    resolution_obj = {
        "action_taken":    action_taken,
        "artifact_ref":    artifact_ref,
        "resolved_at":     resolved_at or received_at,
        "resolution_text": inner.get("resolution_text"),
        "disposition":     disposition,
    }

    matched = False
    for b in blockers:
        if b.get("blocker_id") == target_blocker_id:
            b["status"] = "resolved"
            b["resolution"] = resolution_obj
            matched = True
            break
    if not matched:
        blockers.append({
            "blocker_id":  target_blocker_id,
            "status":      "resolved",
            "description": "(prior to local persistence)",
            "resolution":  resolution_obj,
        })

    change.blockers = json.dumps(blockers)
    db.commit()
    logger.info(
        "Blocker resolution received: change=%s blocker=%s disposition=%s action=%s",
        change.id, target_blocker_id, disposition, action_taken,
    )
    return {"status": "accepted", "message": "Blocker resolution recorded"}
