# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: certification fix rounds (CERT-5).

The approval endpoint here is THE ONLY caller of
`npci_client.send_cert_fix_notification` — the gate the whole loop's safety
rests on. It refuses any round not at `awaiting_approval`, and a failed send
leaves the round parked: telling the bank the authority was notified when it
was not is the worse failure than making the operator click again.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import CertFixRound, PartnerUser
from app.services.cert_remediation import verdicts_to_findings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _round_out(rnd: CertFixRound) -> dict:
    return {
        "id": rnd.id, "change_id": rnd.change_id, "cflow_id": rnd.cflow_id,
        "round_number": rnd.round_number, "status": rnd.status,
        "verdict_case_ids": rnd.verdict_case_ids or [],
        "findings": verdicts_to_findings(rnd.verdicts or []),
        "fix_note": rnd.fix_note, "review_status": rnd.review_status,
        "approved_by": rnd.approved_by,
        "approved_at": rnd.approved_at.isoformat() if rnd.approved_at else None,
    }


@router.get("/changes/{change_id}/cert-report")
def cert_report(change_id: str, user: PartnerUser = Depends(get_current_user),
                db: Session = Depends(get_db)) -> dict:
    """CERT-7, the partner's half: what THIS side knows — its own lifecycle
    timestamps and its fix rounds. The authority's round-by-round diff (fixed
    / still failing / newly failing, coverage as built) lives on the
    authority's report surface; this side does not guess at it."""
    import json

    from app.models import IncomingChange

    change = db.get(IncomingChange, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="unknown change")
    try:
        status_history = json.loads(change.cert_status_history or "{}")
    except ValueError:
        status_history = {}
    rounds = db.execute(
        select(CertFixRound).where(CertFixRound.change_id == change_id)
        .order_by(CertFixRound.round_number)
    ).scalars().all()
    return {
        "change_id": change_id,
        "cert_status": change.cert_status,
        "status_history": status_history,
        "fix_rounds": [_round_out(r) for r in rounds],
    }


@router.get("/changes/{change_id}/cert-fix-rounds")
def list_cert_fix_rounds(change_id: str, user: PartnerUser = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    rounds = db.execute(
        select(CertFixRound).where(CertFixRound.change_id == change_id)
        .order_by(CertFixRound.round_number)
    ).scalars().all()
    return {"rounds": [_round_out(r) for r in rounds]}


@router.post("/changes/{change_id}/cert-fix-rounds/{round_id}/mark-fixed")
def mark_round_fixed(change_id: str, round_id: str,
                     user: PartnerUser = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """The operator asserts the defects are fixed (manually, today — the
    automated path parks at awaiting_manual_fix because cert findings name an
    API/xpath, not a file). Moves the round to awaiting_approval; the send
    still requires the explicit approval below."""
    rnd = db.get(CertFixRound, round_id)
    if rnd is None or rnd.change_id != change_id:
        raise HTTPException(status_code=404, detail="unknown fix round")
    if rnd.status not in ("open", "fixing", "awaiting_manual_fix", "fix_failed"):
        raise HTTPException(
            status_code=409,
            detail=f"round is {rnd.status!r} — nothing to mark fixed")
    rnd.status = "awaiting_approval"
    db.commit()
    return _round_out(rnd)


@router.post("/changes/{change_id}/cert-fix-rounds/{round_id}/approve")
def approve_and_notify(change_id: str, round_id: str,
                       user: PartnerUser = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Approve the round and send `cert_fix_notification` — the ONLY call site.

    THE CLAIM IS AtOMIC AND COMMITTED BEFORE THE SEND. Reading the status and
    then sending was a check-then-act race: two operators (or a double-clicked
    button) both observed `awaiting_approval` and both sent, so the authority
    received duplicate fix notifications and could re-run the round twice. The
    conditional UPDATE below is a compare-and-swap — exactly one caller can
    move the round out of `awaiting_approval`, and the loser gets a 409 having
    sent nothing.

    A failed send REVERTS the claim to `awaiting_approval` rather than leaving
    it approved: the authority must actually have been told before this side
    records that it was.
    """
    from app.models import IncomingChange

    # `change_id` here is this platform's own `IncomingChange.id` — that is what
    # the route carries and what `CertFixRound.change_id` is keyed on. The
    # notification goes ON THE WIRE, where only the authority's id resolves, so
    # translate. Sending the local id had the authority receive a fix
    # notification for a change it had never heard of. Resolved before the
    # claim: a read cannot affect the compare-and-swap, and failing here leaves
    # the round untouched rather than stranded in `approving`.
    change = db.get(IncomingChange, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="unknown change")

    claimed = db.execute(
        update(CertFixRound)
        .where(CertFixRound.id == round_id,
               CertFixRound.change_id == change_id,
               CertFixRound.status == "awaiting_approval")
        .values(status="approving")
    ).rowcount
    db.commit()   # publish the claim BEFORE the send — this is what closes the race

    if not claimed:
        rnd = db.get(CertFixRound, round_id)
        if rnd is None or rnd.change_id != change_id:
            raise HTTPException(status_code=404, detail="unknown fix round")
        raise HTTPException(
            status_code=409,
            detail=f"round is {rnd.status!r}, not awaiting_approval — "
                   "the approval gate only closes on a round that is ready")

    rnd = db.get(CertFixRound, round_id)
    db.refresh(rnd)

    from app.npci_client import send_cert_fix_notification

    try:
        reply = send_cert_fix_notification(
            db, change.npci_change_id,
            fixed_case_ids=list(rnd.verdict_case_ids or []),
            fix_summary=f"Fix round {rnd.round_number}: "
                        f"{len(rnd.verdict_case_ids or [])} case(s) remediated.",
            ready_for_rerun=True,
        )
    except Exception:
        _release_claim(db, round_id)
        logger.exception("cert_fix_round %s: cert_fix_notification raised — "
                         "claim released, round stays awaiting_approval", round_id)
        raise HTTPException(
            status_code=502,
            detail="cert_fix_notification could not be delivered — the round "
                   "remains awaiting approval; retry once connectivity is back")

    if reply is None:
        # send_task returns None on failure — parked, not approved.
        _release_claim(db, round_id)
        logger.error("cert_fix_round %s: cert_fix_notification send failed — "
                     "round stays awaiting_approval", round_id)
        raise HTTPException(
            status_code=502,
            detail="cert_fix_notification could not be delivered — the round "
                   "remains awaiting approval; retry once connectivity is back")

    rnd.status = "approved"
    rnd.approved_by = getattr(user, "username", None) or getattr(user, "id", "operator")
    rnd.approved_at = datetime.now(timezone.utc)
    db.commit()
    return _round_out(rnd)


def _release_claim(db: Session, round_id: str) -> None:
    """Hand the round back to `awaiting_approval` after a failed send, so the
    operator can retry. Conditional on still being `approving` — never stomp a
    status something else has since set."""
    db.rollback()
    db.execute(
        update(CertFixRound)
        .where(CertFixRound.id == round_id, CertFixRound.status == "approving")
        .values(status="awaiting_approval")
    )
    db.commit()
