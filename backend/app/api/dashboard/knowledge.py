# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: knowledge — manage the partner knowledge base (Document
RAG) and (re)index change documents.

KB docs (UPI/IUPI specs, NPCI circulars, past kits, internal standards) are
uploaded here, chunked + embedded into `document_chunks` (doc_category='kb'), and
retrieved cross-change by the design/code/test agents. The change-documents index
is also exposed so an operator can force a re-index.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import IncomingChange, KnowledgeDoc, PartnerUser
from app.rag.doc_ingest import (
    delete_kb_chunks,
    ingest_change_documents,
    ingest_kb_document,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class KnowledgeDocCreate(BaseModel):
    title: str
    content: str
    source: str | None = None


@router.post("/knowledge")
def create_knowledge_doc(
    body: KnowledgeDocCreate,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a knowledge-base document + index it for retrieval."""
    if not body.title.strip() or not body.content.strip():
        raise HTTPException(status_code=400, detail="title and content are required")

    row = KnowledgeDoc(title=body.title.strip(), source=(body.source or None), content=body.content)
    db.add(row)
    db.commit()
    db.refresh(row)

    try:
        n = ingest_kb_document(db, kb_id=row.id, title=row.title, content=row.content)
    except Exception as exc:  # noqa: BLE001 — the doc is saved; indexing can be retried
        logger.warning("KB doc %s saved but indexing failed", row.id, exc_info=True)
        n = 0
    row.chunk_count = n
    db.commit()

    return {"id": row.id, "title": row.title, "source": row.source,
            "chunk_count": n, "created_at": row.created_at.isoformat()}


@router.get("/knowledge")
def list_knowledge_docs(
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List knowledge-base documents (newest first), without their full content."""
    rows = db.execute(
        select(KnowledgeDoc).order_by(KnowledgeDoc.created_at.desc())
    ).scalars().all()
    return [
        {"id": r.id, "title": r.title, "source": r.source,
         "chunk_count": r.chunk_count, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.delete("/knowledge/{kb_id}")
def delete_knowledge_doc(
    kb_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a knowledge-base document + its indexed chunks."""
    row = db.get(KnowledgeDoc, kb_id)
    if row is None:
        raise HTTPException(status_code=404, detail="knowledge doc not found")
    delete_kb_chunks(db, kb_id)
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.post("/changes/{change_id}/documents/index")
def reindex_change_documents(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Force a (re)index of this change's NPCI documents into the Document RAG."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")
    try:
        n = ingest_change_documents(db, change_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"indexing failed — is pgvector/Ollama available? ({exc})",
        )
    return {"change_id": change_id, "chunks_indexed": n}
