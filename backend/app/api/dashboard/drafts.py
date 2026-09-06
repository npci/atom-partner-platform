# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: query drafts — auto-suggested clarification questions."""
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.question_suggester import suggest_questions
from app.api.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import (
    ChangeDocument,
    IncomingChange,
    OutgoingQuery,
    PartnerSetting,
    PartnerUser,
    QueryDraft,
)
from app.npci_client import send_query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _draft_dict(d: QueryDraft) -> dict:
    return {
        "id": d.id,
        "change_id": d.change_id,
        "text": d.text,
        "status": d.status,
        "source": d.source,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "sent_at": d.sent_at.isoformat() if d.sent_at else None,
    }


def _get_setting_value(db: Session, key: str, default: str = "") -> str:
    row = db.get(PartnerSetting, key)
    return row.value if row and row.value else default


@router.post("/changes/{change_id}/queries/suggest")
def suggest_queries_for_change(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Auto-generate clarification question drafts from shared documents.

    Idempotent: if any draft already exists for this change (draft/sent/discarded),
    returns the current draft-status rows without re-invoking the LLM. This makes
    it safe to call on page load.
    """
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    existing_any = db.scalars(
        select(QueryDraft).where(QueryDraft.change_id == change_id)
    ).first()
    if existing_any:
        drafts = db.scalars(
            select(QueryDraft)
            .where(QueryDraft.change_id == change_id, QueryDraft.status == "draft")
            .order_by(QueryDraft.created_at)
        ).all()
        return {"generated": False, "reason": "already_generated", "drafts": [_draft_dict(d) for d in drafts]}

    api_key = _get_setting_value(db, "partner_anthropic_api_key", "") or settings.partner_anthropic_api_key
    if not api_key:
        return {"generated": False, "reason": "no_api_key", "drafts": []}

    # Version-aware context: v1 baseline + LLM summary of later-version changes
    # (option C), so suggested questions factor in what NPCI revised.
    from app.agents.revision_context import assemble_change_context
    ctx = assemble_change_context(db, change_id, api_key)
    if not ctx["documents"]:
        return {"generated": False, "reason": "no_documents", "drafts": []}

    questions = suggest_questions(
        api_key=api_key,
        change_title=change.title,
        documents=ctx["documents"],
        revision_summary=ctx["revision_summary"],
    )

    if not questions:
        return {"generated": False, "reason": "no_questions_produced", "drafts": []}

    created: list[QueryDraft] = []
    for q in questions:
        d = QueryDraft(change_id=change_id, text=q, status="draft", source="auto")
        db.add(d)
        created.append(d)
    db.commit()
    for d in created:
        db.refresh(d)

    logger.info("Draft questions generated: change=%s count=%d", change_id, len(created))
    return {"generated": True, "drafts": [_draft_dict(d) for d in created]}


@router.get("/changes/{change_id}/query-drafts")
def list_query_drafts(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    drafts = db.scalars(
        select(QueryDraft)
        .where(QueryDraft.change_id == change_id, QueryDraft.status == "draft")
        .order_by(QueryDraft.created_at)
    ).all()
    return [_draft_dict(d) for d in drafts]


class DraftUpdateRequest(BaseModel):
    text: str


@router.patch("/query-drafts/{draft_id}")
def update_query_draft(
    draft_id: str,
    body: DraftUpdateRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    d = db.get(QueryDraft, draft_id)
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    if d.status != "draft":
        raise HTTPException(status_code=400, detail="Cannot edit a draft that has been sent or discarded")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Draft text cannot be empty")
    d.text = text
    db.commit()
    db.refresh(d)
    return _draft_dict(d)


@router.delete("/query-drafts/{draft_id}")
def discard_query_draft(
    draft_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    d = db.get(QueryDraft, draft_id)
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    d.status = "discarded"
    db.commit()
    return {"discarded": True}


@router.post("/query-drafts/{draft_id}/send")
def send_query_draft(
    draft_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    d = db.get(QueryDraft, draft_id)
    if not d:
        raise HTTPException(status_code=404, detail="Draft not found")
    if d.status != "draft":
        raise HTTPException(status_code=400, detail="Draft has already been sent or discarded")

    change = db.get(IncomingChange, d.change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    correlation_id = str(uuid4())
    result = send_query(db, change.npci_change_id, d.text, correlation_id=correlation_id)

    outgoing = OutgoingQuery(
        change_id=d.change_id,
        message=d.text,
        status="sent" if result else "failed",
        correlation_id=correlation_id,
    )
    db.add(outgoing)

    d.status = "sent"
    d.sent_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("Draft sent: draft=%s change=%s sent_to_npci=%s", draft_id, d.change_id, result is not None)
    return {"sent": result is not None, "query_id": outgoing.id}
