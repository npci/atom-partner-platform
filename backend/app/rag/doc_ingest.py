"""Document RAG ingestion — chunk + embed + store NPCI change documents and
partner knowledge-base documents into the shared `document_chunks` pgvector
table (doc_category 'change_doc' / 'kb').

Re-ingest is idempotent: the prior chunks for the same scope are deleted first,
then the fresh chunks inserted. Raw SQL (psycopg + pgvector) — vectors are bound
as their text literal and CAST to `vector`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlalchemy import text as _sql
from sqlalchemy.orm import Session

from app.rag.doc_chunker import chunk_text
from app.rag.embed_cache import embed_texts_cached
from app.rag.embeddings import is_zero, vector_literal

logger = logging.getLogger(__name__)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _store_chunks(
    db: Session,
    *,
    doc_category: str,
    source_key: str,
    chunks: list[str],
    change_id: str | None = None,
    repo_id: str | None = None,
    base_metadata: dict | None = None,
) -> int:
    """Embed + insert `chunks`. Skips chunks whose embedding hard-failed
    (all-zero). Returns the number of rows written."""
    if not chunks:
        return 0
    vectors = embed_texts_cached(chunks)
    written = 0
    for idx, (content, vec) in enumerate(zip(chunks, vectors)):
        if is_zero(vec):
            continue
        meta = dict(base_metadata or {})
        meta["chunk_index"] = idx
        db.execute(
            _sql(
                "INSERT INTO document_chunks "
                "(id, doc_category, source_key, change_id, repo_id, chunk_index, "
                " content, content_sha256, embedding, metadata) "
                "VALUES (:id, :cat, :sk, :cid, :rid, :ci, :content, :sha, "
                " CAST(:emb AS vector), CAST(:meta AS jsonb))"
            ),
            {
                "id": str(uuid.uuid4()),
                "cat": doc_category,
                "sk": source_key,
                "cid": change_id,
                "rid": repo_id,
                "ci": idx,
                "content": content,
                "sha": _sha(content),
                "emb": vector_literal(vec),
                "meta": json.dumps(meta),
            },
        )
        written += 1
    return written


def _delete_chunks(db: Session, *, doc_category: str, change_id: str | None = None,
                   source_key: str | None = None, repo_id: str | None = None) -> None:
    clauses = ["doc_category = :cat"]
    params: dict = {"cat": doc_category}
    if change_id is not None:
        clauses.append("change_id = :cid")
        params["cid"] = change_id
    if source_key is not None:
        clauses.append("source_key = :sk")
        params["sk"] = source_key
    if repo_id is not None:
        clauses.append("repo_id = :rid")
        params["rid"] = repo_id
    db.execute(_sql(f"DELETE FROM document_chunks WHERE {' AND '.join(clauses)}"), params)


def ingest_change_documents(db: Session, change_id: str) -> int:
    """(Re)index the NPCI documents for one change. Returns chunks written.

    Indexes the latest version of each doc_type so retrieval doesn't surface
    superseded revisions. Idempotent — wipes the change's prior change_doc chunks
    first."""
    from app.models import ChangeDocument

    rows = db.execute(
        _sql(
            "SELECT doc_type, content, version FROM change_documents "
            "WHERE change_id = :cid ORDER BY version DESC"
        ),
        {"cid": change_id},
    ).fetchall()
    # Latest version per doc_type.
    latest: dict[str, tuple[str, int]] = {}
    for doc_type, content, version in rows:
        if doc_type not in latest:
            latest[doc_type] = (content or "", version)

    _delete_chunks(db, doc_category="change_doc", change_id=change_id)

    total = 0
    for doc_type, (content, version) in latest.items():
        chunks = chunk_text(content)
        total += _store_chunks(
            db,
            doc_category="change_doc",
            source_key=doc_type,
            chunks=chunks,
            change_id=change_id,
            base_metadata={"doc_type": doc_type, "version": version},
        )
    db.commit()
    logger.info("ingest_change_documents: change=%s doc_types=%d chunks=%d",
                change_id, len(latest), total)
    _ = ChangeDocument  # imported for clarity that this indexes that table
    return total


def ensure_change_indexed(db: Session, change_id: str) -> None:
    """Index the change's docs if not already present. Cheap idempotent guard the
    agent-retrieval path calls before retrieving."""
    n = db.execute(
        _sql("SELECT 1 FROM document_chunks WHERE doc_category='change_doc' AND change_id=:cid LIMIT 1"),
        {"cid": change_id},
    ).first()
    if n is None:
        ingest_change_documents(db, change_id)


def ingest_kb_document(db: Session, *, kb_id: str, title: str, content: str) -> int:
    """(Re)index one knowledge-base document. Idempotent per kb_id."""
    _delete_chunks(db, doc_category="kb", source_key=kb_id)
    chunks = chunk_text(content)
    n = _store_chunks(
        db, doc_category="kb", source_key=kb_id, chunks=chunks,
        base_metadata={"kb_id": kb_id, "title": title},
    )
    db.commit()
    logger.info("ingest_kb_document: kb=%s chunks=%d", kb_id, n)
    return n


def delete_kb_chunks(db: Session, kb_id: str) -> None:
    _delete_chunks(db, doc_category="kb", source_key=kb_id)
    db.commit()
