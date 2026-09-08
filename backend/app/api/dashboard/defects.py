# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: defects — partner-reported implementation blockers."""
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import IncomingChange, PartnerUser
from app.authority_client import send_blocker, send_emergency_issue

from .changes import get_change

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class BlockerOption(BaseModel):
    option: str
    eta: str | None = None
    impact: str | None = None


class BlockerRequest(BaseModel):
    severity: str             # critical / high / medium / low
    description: str
    impact: str | None = None
    investigation_done: list[str] = []
    options_considered: list[BlockerOption] = []

    # Renamed from `requested_action_from_npci`. This is THIS platform's own
    # REST API — its only client is this repo's SPA — so unlike the A2A payload
    # key of the same name, it is not a contract with a separately-deployed
    # counterparty and can be renamed here.
    #
    # The old spelling stays as a validation alias rather than being deleted: an
    # operator's script or a browser tab open across the upgrade would otherwise
    # start silently dropping the field, and a blocker filed with an empty
    # requested action reads as "no action wanted" rather than as a failed
    # request.
    requested_action_from_authority: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "requested_action_from_authority", "requested_action_from_npci"),
    )

    model_config = ConfigDict(populate_by_name=True)


@router.post("/changes/{change_id}/blocker")
def report_blocker(
    change_id: str,
    body: BlockerRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partner reports an obstacle blocking implementation. Per the
    rollout-doc Journey C: structured severity + impact + investigation
    + options the authority can pick from. Wire format = task_type='blocker'."""
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="Description cannot be empty")
    if body.severity not in ("critical", "high", "medium", "low"):
        raise HTTPException(status_code=400, detail="severity must be critical/high/medium/low")

    blocker_id = f"BLK-{uuid4().hex[:8].upper()}"
    options_payload = [o.model_dump() for o in body.options_considered]
    result = send_blocker(
        db, change.authority_change_id,
        blocker_id=blocker_id,
        severity=body.severity,
        description=body.description,
        impact=body.impact,
        investigation_done=body.investigation_done,
        options_considered=options_payload,
        requested_action_from_authority=body.requested_action_from_authority,
    )
    if not result:
        raise HTTPException(
            status_code=502, detail="Failed to deliver blocker to the authority")

    # Persist locally so the Blockers section can render history +
    # the resolution the authority sends back later.
    import json
    from datetime import datetime, timezone
    existing = []
    if change.blockers:
        try:
            existing = json.loads(change.blockers) or []
        except (json.JSONDecodeError, TypeError):
            existing = []
    existing.append({
        "blocker_id":                  blocker_id,
        "severity":                    body.severity,
        "description":                 body.description,
        "impact":                      body.impact,
        "investigation_done":          body.investigation_done,
        "options_considered":          options_payload,
        # Written under the neutral key from here on. Rows persisted before this
        # change still carry `requested_action_from_npci`, so any reader must
        # accept both — the store is append-only and is never rewritten.
        "requested_action_from_authority": body.requested_action_from_authority,
        "status":                      "open",
        "resolution":                  None,
        "created_at":                  datetime.now(timezone.utc).isoformat(),
    })
    change.blockers = json.dumps(existing)
    db.commit()

    logger.info("Blocker reported: change=%s by=%s id=%s severity=%s",
                change_id, user.username, blocker_id, body.severity)
    return {
        "sent": True,
        "blocker_id": blocker_id,
        "change": get_change(change_id, user, db),
    }


class EmergencyIssueRequest(BaseModel):
    title: str
    description: str
    severity: str = "critical"   # critical / high / medium / low


@router.post("/changes/{change_id}/emergency-issue")
def raise_emergency_issue(
    change_id: str,
    body: EmergencyIssueRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Post-freeze break-glass channel. Only meaningful once the authority
    has shipped the final kit version and frozen the change — at which point
    queries/counters are rejected and this is the only way to flag a
    work-stopping problem. Wire format = task_type='emergency_issue'."""
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if not body.title.strip() or not body.description.strip():
        raise HTTPException(status_code=400, detail="Title and description are required")
    if body.severity not in ("critical", "high", "medium", "low"):
        raise HTTPException(status_code=400, detail="severity must be critical/high/medium/low")

    issue_id = f"EMG-{uuid4().hex[:8].upper()}"
    result = send_emergency_issue(
        db, change.authority_change_id,
        issue_id=issue_id,
        severity=body.severity,
        title=body.title,
        description=body.description,
    )
    if not result:
        raise HTTPException(status_code=502, detail="Failed to deliver emergency issue to the authority")

    import json
    from datetime import datetime, timezone
    existing = []
    if change.emergency_issues:
        try:
            existing = json.loads(change.emergency_issues) or []
        except (json.JSONDecodeError, TypeError):
            existing = []
    existing.append({
        "issue_id":    issue_id,
        "severity":    body.severity,
        "title":       body.title,
        "description": body.description,
        "status":      "open",
        "resolution":  None,
        "created_at":  datetime.now(timezone.utc).isoformat(),
    })
    change.emergency_issues = json.dumps(existing)
    db.commit()

    logger.info("Emergency issue raised: change=%s by=%s id=%s severity=%s",
                change_id, user.username, issue_id, body.severity)
    return {
        "sent": True,
        "issue_id": issue_id,
        "change": get_change(change_id, user, db),
    }
