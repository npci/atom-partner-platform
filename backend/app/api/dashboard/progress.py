# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: progress — Design → Coding → Testing milestone reporting."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import IncomingChange, PartnerUser, ProgressReport
from app.authority_client import report_progress

from ._shared import _CERT_STATUS_ORDER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class ProgressRequest(BaseModel):
    step: str
    notes: str = ""


@router.post("/changes/{change_id}/progress")
def submit_progress(change_id: str, body: ProgressRequest, user: PartnerUser = Depends(get_current_user), db: Session = Depends(get_db)):
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    valid_steps = ["design_completed", "coding_completed", "testing_completed"]
    if body.step not in valid_steps:
        raise HTTPException(status_code=400, detail=f"Invalid step. Valid: {valid_steps}")

    # Check sequential
    existing = db.scalars(select(ProgressReport).where(ProgressReport.change_id == change_id)).all()
    reported = {p.step for p in existing}

    idx = valid_steps.index(body.step)
    for prev in valid_steps[:idx]:
        if prev not in reported:
            raise HTTPException(status_code=400, detail=f"Must report '{prev}' first")

    if body.step in reported:
        return {"already_reported": True}

    # Send to the authority. Like every `send_task` caller, a delivery failure
    # comes back as None (queued for retry) rather than an exception.
    result = report_progress(db, change.authority_change_id, body.step, body.notes)
    if result is None:
        # The local ProgressReport row below is written regardless, and
        # `submit_readiness` gates ONLY on those local rows. So an undelivered
        # milestone still lets readiness be declared later. The retry sweeper
        # normally closes that gap, but it abandons a row after
        # `outbound_retry_max_attempts` — log loudly so an operator can see
        # which milestone the authority may never have received.
        logger.warning(
            "Milestone NOT delivered (queued for retry): change=%s step=%s",
            change_id, body.step,
        )

    # Save locally
    db.add(ProgressReport(change_id=change_id, step=body.step))
    change.status = "in_progress"
    cert_sent = None

    # Auto-advance cert_status to mirror the Implementation Pipeline
    # milestones. The partner UI hides the redundant cert-stage clicks
    # for 'deployed' and 'tested' (those concerns are owned by the
    # Implementation Pipeline now) but the underlying cert lifecycle
    # still needs those states for downstream consumers and audit.
    #   coding_completed   → cert_status = deployed
    #   testing_completed  → cert_status = tested
    # Each transition fires a real cert_status_update A2A so the authority sees
    # the full lifecycle, not just a jump to ready_for_certification.
    STEP_TO_CERT = {
        "coding_completed":  "deployed",
        "testing_completed": "tested",
    }
    target_cert = STEP_TO_CERT.get(body.step)
    if target_cert:
        import json as _json
        from datetime import datetime, timezone

        from app.authority_client import send_cert_status_update
        current_idx = _CERT_STATUS_ORDER.index(change.cert_status or "received")
        target_idx  = _CERT_STATUS_ORDER.index(target_cert)
        if target_idx > current_idx:
            history = {}
            if change.cert_status_history:
                try:
                    history = _json.loads(change.cert_status_history) or {}
                except Exception:
                    history = {}
            history[target_cert] = datetime.now(timezone.utc).isoformat()
            change.cert_status = target_cert
            change.cert_status_history = _json.dumps(history)
            try:
                # `send_cert_status_update` -> `send_task` reports a delivery
                # failure by RETURNING None; it swallows transport/auth errors
                # internally (authority_client.send_task_async) and never raises
                # for them. So the `except` below cannot see a failed send — the
                # None check is the one that actually fires. Logging success
                # unconditionally here previously made an undelivered update
                # read as delivered in the log.
                cert_sent = send_cert_status_update(
                    db, change.authority_change_id, target_cert, role="", test_data={}
                )
                if cert_sent is None:
                    # Queued in OutboundA2ARetry for redelivery, NOT lost — but
                    # the sweeper abandons a row after `outbound_retry_max_attempts`,
                    # so this is best-effort, not guaranteed. Local cert_status
                    # stays advanced (the retry will carry it) and we say so.
                    logger.warning(
                        "Auto cert_status advance NOT delivered (queued for retry): "
                        "change=%s step=%s → cert=%s",
                        change_id, body.step, target_cert,
                    )
                else:
                    logger.info("Auto cert_status advance: change=%s step=%s → cert=%s",
                                change_id, body.step, target_cert)
            except Exception:
                # Retained for the non-delivery failure modes that DO raise
                # (e.g. send_task called from a running event loop). Delivery
                # failures take the `cert_sent is None` branch above.
                cert_sent = None
                logger.warning("Auto cert_status_update raised", exc_info=True)

    db.commit()

    logger.info("Progress reported: change=%s step=%s", change_id, body.step)
    return {
        "reported":               True,
        "sent_to_npci":           result is not None,
        # Distinct from `sent_to_npci`: the milestone and the mirrored
        # cert_status flip are two separate A2A sends and either can fail
        # alone. None when this step maps to no cert_status transition.
        "cert_status_sent_to_npci": None if target_cert is None else cert_sent is not None,
    }
