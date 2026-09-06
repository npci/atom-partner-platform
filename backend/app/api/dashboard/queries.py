# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: queries — general + cert-channel clarifications to NPCI."""
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import IncomingChange, OutgoingQuery, PartnerUser
from app.npci_client import send_query

from .changes import get_change

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _is_frozen(change) -> bool:
    """A frozen change has shipped its final kit — there is no next version, so a
    leftover revision-hold flag is moot and must not block clarifying queries."""
    return (
        getattr(change, "negotiation_finalized_at", None) is not None
        or (getattr(change, "negotiation_version", 1) or 1) >= 3
    )


class QueryRequest(BaseModel):
    message: str


@router.post("/changes/{change_id}/query")
def submit_query(change_id: str, body: QueryRequest, user: PartnerUser = Depends(get_current_user), db: Session = Depends(get_db)):
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if getattr(change, "revision_in_progress", False) and not _is_frozen(change):
        raise HTTPException(
            status_code=409,
            detail="NPCI is preparing a revised kit — queries are on hold until the new version ships.",
        )

    # Mint correlation_id first so we can pass the same UUID to both the
    # OutgoingQuery row and the A2A wire — NPCI echoes it back on the
    # CLARIFICATION_RESPONSE so the answer attaches to THIS row, not
    # whichever pending query is most recent in the channel.
    correlation_id = str(uuid4())

    # Send to NPCI via A2A — task_type=query, kind=general
    result = send_query(db, change.npci_change_id, body.message, correlation_id=correlation_id)

    query = OutgoingQuery(
        change_id=change_id,
        message=body.message,
        kind="general",
        status="sent" if result else "failed",
        correlation_id=correlation_id,
    )
    db.add(query)
    db.commit()

    logger.info("Query submitted: change=%s kind=general len=%d", change_id, len(body.message))
    # Return the freshly-built change blob so the partner UI can hydrate
    # its query cache via `setQueryData` instead of a separate refetch —
    # eliminates the post-send refetch lag (formerly 0-5 s).
    return {
        "sent": result is not None,
        "query_id": query.id,
        "change": get_change(change_id, user, db),
    }


@router.post("/changes/{change_id}/cert-query")
def submit_cert_query(
    change_id: str,
    body: QueryRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a cert-channel clarification to NPCI.

    Same wire shape as `/query` but uses A2A task_type=cert_query so
    NPCI's executor routes it to a kind='cert' negotiation thread.
    Strictly separate from the general Phase C inbox — no row is shared.
    """
    from app.npci_client import send_cert_query

    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if getattr(change, "revision_in_progress", False) and not _is_frozen(change):
        raise HTTPException(
            status_code=409,
            detail="NPCI is preparing a revised kit — queries are on hold until the new version ships.",
        )

    correlation_id = str(uuid4())
    result = send_cert_query(
        db, change.npci_change_id, body.message, correlation_id=correlation_id,
    )

    query = OutgoingQuery(
        change_id=change_id,
        message=body.message,
        kind="cert",
        status="sent" if result else "failed",
        correlation_id=correlation_id,
    )
    db.add(query)
    db.commit()

    logger.info("Cert query submitted: change=%s len=%d", change_id, len(body.message))
    return {"sent": result is not None, "query_id": query.id}


@router.get("/changes/{change_id}/cert-queries")
def list_cert_queries(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return cert-channel queries (and any responses) for the partner UI inbox."""
    from sqlalchemy import select
    rows = db.scalars(
        select(OutgoingQuery)
        .where(OutgoingQuery.change_id == change_id, OutgoingQuery.kind == "cert")
        .order_by(OutgoingQuery.sent_at)
    ).all()
    return {
        "queries": [
            {
                "id": q.id,
                "message": q.message,
                "response": q.response,
                "status": q.status,
                "sent_at": q.sent_at.isoformat() if q.sent_at else None,
                "response_received_at": q.response_received_at.isoformat() if q.response_received_at else None,
            }
            for q in rows
        ]
    }
