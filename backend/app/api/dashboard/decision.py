# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: decision — rollout accept / counter-propose / counter-accept.

These emit the rollout-contract A2A messages (PROPOSAL_ACCEPTANCE,
COUNTER_PROPOSAL, COUNTER_DECISION) and maintain the npci_counter audit trail.
"""
import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import IncomingChange, OutgoingQuery, PartnerUser
from app.npci_client import send_counter_decision, send_counter_proposal, send_proposal_acceptance

from .changes import get_change

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _archive_npci_counter(change, resolution: str, response_text: str = "") -> None:
    """Append the current `npci_counter` snapshot to `npci_counter_history`
    with how the partner resolved it, then clear the active slot.

    Call BEFORE setting `change.npci_counter = None`. No-op if there's
    no active counter to archive. `resolution` is one of:
      'countered'         — partner sent their own counter back
      'accepted'          — partner explicitly accepted NPCI's counter
                            via /counter-proposals/{cp_id}/accept; rollout
                            decision is NOT advanced (partner still needs
                            to explicitly accept the rollout afterward)
      'superseded'        — partner accepted the rollout on original terms
                            after a frontend warning; counter terms didn't
                            take effect (NOT an implicit acceptance)
      'accepted_rollout'  — legacy alias for the pre-warning behavior where
                            rollout-accept silently archived the counter as
                            accepted. New writes use 'superseded'.
    """
    if not change.npci_counter:
        return
    try:
        snapshot = json.loads(change.npci_counter)
    except (json.JSONDecodeError, TypeError):
        return
    snapshot["status"] = "responded"
    snapshot["resolution"] = resolution
    snapshot["response_text"] = response_text
    snapshot["resolved_at"] = datetime.now(timezone.utc).isoformat()

    history: list = []
    if change.npci_counter_history:
        try:
            existing = json.loads(change.npci_counter_history)
            if isinstance(existing, list):
                history = existing
        except (json.JSONDecodeError, TypeError):
            pass
    history.append(snapshot)
    change.npci_counter_history = json.dumps(history)


class AcceptChangeRequest(BaseModel):
    """Optional fields the partner can fill on the Accept dialog."""
    internal_change_advisory_ref: str | None = None
    implementation_kickoff_date: str | None = None
    estimated_phase_timeline: dict | None = None


@router.post("/changes/{change_id}/accept")
def accept_change(
    change_id: str,
    body: AcceptChangeRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partner formally accepts the rollout. Wire format mirrors the
    rollout-doc PROPOSAL_ACCEPTANCE."""
    logger.info(
        "Rollout accept: change=%s by=%s",
        change_id, getattr(user, "username", "?"),
    )
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if change.decision == "accepted":
        return {"sent": True, "decision": "accepted", "message": "Already accepted"}

    # `accepted_by` derived from the logged-in PartnerUser. Username is
    # the stable handle; full_name is preferred when set.
    accepted_by = {
        "name":  getattr(user, "full_name", None) or user.username,
        "username": user.username,
        "role":  getattr(user, "role", None),
    }

    # The kit_id we received is per-doc on the inbound; we don't store it
    # explicitly today, so derive it from npci_change_id (matches the
    # NPCI convention `CHG_<change_id>`).
    kit_id = f"CHG_{change.npci_change_id}"

    result = send_proposal_acceptance(
        db, change.npci_change_id, kit_id,
        accepted_by=accepted_by,
        internal_change_advisory_ref=body.internal_change_advisory_ref,
        estimated_phase_timeline=body.estimated_phase_timeline,
        implementation_kickoff_date=body.implementation_kickoff_date,
    )
    if not result:
        raise HTTPException(status_code=502, detail="Failed to deliver acceptance to NPCI")

    change.decision = "accepted"
    # Partner accepted the rollout on the original terms — any open NPCI
    # counter is now moot (its terms didn't take effect). Archive with
    # resolution="superseded" rather than "accepted" so the audit trail
    # doesn't read as an implicit counter acceptance. The frontend warns
    # the partner before this state transition.
    _archive_npci_counter(
        change,
        resolution="superseded",
        response_text="Counter superseded — rollout accepted on the original terms",
    )
    change.npci_counter = None
    db.commit()

    logger.info("Change accepted: change=%s by=%s", change_id, user.username)
    return {"sent": True, "decision": "accepted"}


class CounterProposalRequest(BaseModel):
    """Body for POST /changes/{id}/counter — partner's structured
    negotiation request. Includes a required justification plus optional
    category and per-category structured payload."""
    justification: str
    # Structured category from the new negotiation form.
    # timeline | scope | limits | api_contract | dependency | cert_role
    request_category: str | None = None
    # Per-category structured fields — shape varies by category.
    # See OutgoingQuery.request_payload docstring for exact shapes.
    request_payload: dict | None = None


@router.post("/changes/{change_id}/counter")
def counter_propose(
    change_id: str,
    body: CounterProposalRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partner sends a counter-proposal. Routed via the QUERY transport
    so NPCI's existing negotiation thread + AI-draft response pipeline
    handles it; the `message_kind: COUNTER_PROPOSAL` discriminator on
    the payload tells NPCI it's a counter, not a clarifying question."""
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if getattr(change, "revision_in_progress", False):
        raise HTTPException(
            status_code=409,
            detail="NPCI is preparing a revised kit — counter-proposals are on hold until the new version ships.",
        )
    if change.decision == "accepted":
        raise HTTPException(status_code=400, detail="Already accepted; cannot counter-propose")
    if not body.justification.strip():
        raise HTTPException(status_code=400, detail="Justification cannot be empty")

    counter_proposal_id = f"counter-{uuid4().hex[:12]}"
    kit_id = f"CHG_{change.npci_change_id}"

    import json as _json
    result = send_counter_proposal(
        db, change.npci_change_id, kit_id,
        counter_proposal_id=counter_proposal_id,
        justification=body.justification,
        request_category=body.request_category,
        request_payload=body.request_payload,
    )
    if not result:
        raise HTTPException(status_code=502, detail="Failed to deliver counter-proposal to NPCI")

    # Mirror the local thread so the partner sees their own message in
    # Activity. kind='negotiation' tags the row so the partner UI's
    # events-builder routes the bubble to the Negotiation tab instead
    # of General. The NPCI side already sees this as a NegotiationMessage
    # with role='counter_proposal' (from process_counter_proposal), which
    # the npciTabOf categorizer maps to its Negotiation tab.
    db.add(OutgoingQuery(
        change_id=change_id,
        message=body.justification,
        status="sent",
        kind="negotiation",
        request_category=body.request_category,
        request_payload=_json.dumps(body.request_payload) if body.request_payload else None,
    ))

    change.decision = "negotiating"
    # Partner has now responded to NPCI's prior counter (if any).
    # Archive its snapshot first so the chat timeline keeps showing it,
    # then clear the active slot so the "needs action" card disappears.
    _archive_npci_counter(
        change,
        resolution="countered",
        response_text=body.justification,
    )
    change.npci_counter = None
    db.commit()

    logger.info("Counter-proposal sent: change=%s by=%s id=%s", change_id, user.username, counter_proposal_id)
    return {
        "sent": True,
        "decision": "negotiating",
        "counter_proposal_id": counter_proposal_id,
        "change": get_change(change_id, user, db),
    }


@router.post("/changes/{change_id}/counter-proposals/{cp_id}/accept")
def accept_counter_proposal(
    change_id: str,
    cp_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partner accepts a specific NPCI-originated counter — resolves
    just that negotiation thread. Distinct from `/accept` (which is
    the rollout-level acceptance and emits PROPOSAL_ACCEPTANCE).

    On success: archives the active `npci_counter` with
    `resolution='accepted'` and emits a COUNTER_DECISION (decision=ACCEPT)
    over A2A so NPCI flips its CounterProposal row to ACCEPTED. The
    rollout decision (`change.decision`) is intentionally NOT modified;
    partner must click Accept rollout separately.
    """
    logger.info(
        "Counter accept: cp=%s change=%s by=%s",
        cp_id, change_id, getattr(user, "username", "?"),
    )
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if not change.npci_counter:
        raise HTTPException(status_code=400, detail="No open NPCI counter to accept")
    try:
        active = json.loads(change.npci_counter)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=500, detail="Corrupt npci_counter state")
    active_cp_id = active.get("counter_proposal_id")
    if active_cp_id != cp_id:
        raise HTTPException(
            status_code=409,
            detail=f"Counter '{cp_id}' is not the open counter (open={active_cp_id})",
        )

    kit_id = f"CHG_{change.npci_change_id}"
    resolution_text = f"Counter terms accepted by {getattr(user, 'full_name', None) or user.username}"
    result = send_counter_decision(
        db, change.npci_change_id, kit_id,
        counter_proposal_id=cp_id,
        decision="ACCEPT",
        resolution_text=resolution_text,
    )
    if not result:
        raise HTTPException(status_code=502, detail="Failed to deliver counter decision to NPCI")

    _archive_npci_counter(change, resolution="accepted", response_text=resolution_text)
    change.npci_counter = None
    db.commit()

    logger.info("NPCI counter accepted: change=%s cp=%s by=%s", change_id, cp_id, user.username)
    return {"sent": True, "counter_proposal_id": cp_id, "decision": change.decision}


@router.post("/changes/{change_id}/accept-version")
def accept_negotiation_version(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partner explicitly accepts the current negotiation version (v2, v3…).

    Called when partner clicks "Accept New Version" on the new-version banner.
    Sets negotiation_version_accepted = True on the IncomingChange so the
    banner is dismissed.
    """
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    change.negotiation_version_accepted = True
    db.commit()
    logger.info(
        "Partner accepted negotiation version %s: change=%s by=%s",
        change.negotiation_version, change_id, user.username,
    )
    return {
        "accepted_version": change.negotiation_version,
        "change_id": change_id,
    }
