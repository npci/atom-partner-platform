"""Retrieval over the Document RAG store (`document_chunks`).

Cosine search via pgvector's `<=>` operator, scoped by category:
  - 'change_doc' → scoped to a specific change_id
  - 'kb'         → the partner knowledge base (cross-change)
  - 'code'       → reserved for the Code RAG (Phase 3.2; needs a repo_id scope)

Fail-soft: any error (pgvector absent, bad query embedding) returns [] so an
agent degrades to its existing full-document context rather than erroring.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text as _sql
from sqlalchemy.orm import Session

from app.rag.embed_cache import embed_texts_cached
from app.rag.embeddings import is_zero, vector_literal

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8


def retrieve(
    db: Session,
    query: str,
    *,
    change_id: str | None = None,
    repo_id: str | None = None,
    categories: tuple[str, ...] = ("change_doc", "kb"),
    top_k: int = DEFAULT_TOP_K,
) -> list[dict]:
    """Return up to `top_k` chunks most similar to `query`, each:
    { content, doc_category, source_key, score, metadata }.

    `change_doc` results require `change_id`; `kb` results are cross-change;
    `code` results require `repo_id` (scoped to one registered repo)."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        qvec = embed_texts_cached([query])[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("retrieve: query embed failed", exc_info=True)
        return []
    if is_zero(qvec):
        return []

    clauses: list[str] = []
    params: dict = {"q": vector_literal(qvec), "k": int(top_k)}
    if "change_doc" in categories and change_id:
        clauses.append("(doc_category = 'change_doc' AND change_id = :cid)")
        params["cid"] = change_id
    if "kb" in categories:
        clauses.append("doc_category = 'kb'")
    if "code" in categories and repo_id:
        clauses.append("(doc_category = 'code' AND repo_id = :rid)")
        params["rid"] = repo_id
    if not clauses:
        return []

    where = " OR ".join(clauses)
    sql = (
        "SELECT content, doc_category, source_key, metadata::text, "
        "(embedding <=> CAST(:q AS vector)) AS dist "
        f"FROM document_chunks WHERE {where} "
        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
    )
    try:
        rows = db.execute(_sql(sql), params).fetchall()
    except Exception as e:  # noqa: BLE001 — fail-soft to no-context
        logger.warning("retrieve: query failed (returning no context)", exc_info=True)
        return []

    out: list[dict] = []
    for content, cat, source_key, meta_text, dist in rows:
        try:
            meta = json.loads(meta_text) if meta_text else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        out.append({
            "content": content,
            "doc_category": cat,
            "source_key": source_key,
            "score": round(1.0 - float(dist), 4),  # cosine similarity
            "metadata": meta,
        })
    return out


def build_kb_context(db: Session, query: str, *, top_k: int = 6, max_chars: int = 8000) -> str:
    """Retrieve knowledge-base chunks relevant to `query` and format them as a
    prompt block. KB-only (the agents already carry the full current-change docs;
    this augments them with cross-change prior knowledge). Fail-soft → ""."""
    chunks = retrieve(db, query, categories=("kb",), top_k=top_k)
    return build_context(chunks, max_chars=max_chars)


def build_code_context(db: Session, query: str, *, repo_id: str,
                       top_k: int = 8, max_chars: int = 10000) -> str:
    """Retrieve source-code chunks from one registered repo relevant to `query`
    and format them as a prompt block. This is what makes the code agent
    *grounded* — the excerpts carry real file paths + symbols. Fail-soft → ""."""
    if not repo_id:
        return ""
    chunks = retrieve(db, query, categories=("code",), repo_id=repo_id, top_k=top_k)
    return build_context(chunks, max_chars=max_chars)


def build_context(chunks: list[dict], *, max_chars: int = 12000) -> str:
    """Format retrieved chunks into a labelled prompt block, bounded to
    `max_chars`. Empty string when there's nothing to add."""
    if not chunks:
        return ""
    parts: list[str] = []
    total = 0
    for c in chunks:
        cat = c.get("doc_category", "doc")
        meta = c.get("metadata") or {}
        label = meta.get("title") or meta.get("doc_type") or c.get("source_key") or cat
        header = f"[{cat}: {label}] (relevance {c.get('score')})"
        piece = f"{header}\n{c.get('content', '')}"
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n---\n\n".join(parts)
