# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

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

# ── The retrieval query: ONE static statement (sonatype-2021-0025 hardening) ──
#
# WHY THIS IS A CONSTANT AND NOT BUILT PER CALL
#
# This query previously assembled its WHERE clause at call time by joining a
# list of predicate fragments with " OR " and interpolating the result into the
# SQL via an f-string. As in `doc_ingest._delete_chunks`, that was safe at
# runtime — the fragments were hardcoded and all values were bound — but it is
# not safe by construction, and it is exactly the dynamic-SQL-assembly pattern
# Sonatype's sonatype-2021-0025 advisory reports against SQLAlchemy. That
# advisory ships no fixed version, so no dependency bump can ever clear it; the
# only thing that clears it is not building SQL from strings.
#
# THE TECHNIQUE: predicate toggles instead of predicate assembly.
#
# The three scopes are independent and OR'd, which would need 7 enumerated
# statements to cover exhaustively — enough to be unreadable and easy to get
# wrong. So instead of choosing WHICH SQL to run, the caller binds three boolean
# flags that switch branches on and off inside one fixed statement:
#
#     (:want_kb AND doc_category = 'kb') OR ...
#
# When `:want_kb` is false the branch is unsatisfiable and contributes no rows —
# identical results to omitting the fragment, but the SQL text never changes.
#
# QUERY-PLAN NOTE — WHAT IS CLAIMED AND WHAT IS NOT.
#
# The flags are bound as real booleans (not strings), so no per-row cast is
# introduced. Beyond that, be careful about what is asserted here: an earlier
# revision of this comment claimed Postgres "folds the false branches away
# during planning" and that "the partial indexes on doc_category / change_id /
# repo_id are still used". Neither claim was verified, and the second is simply
# false — `_ensure_document_chunks_table()` in app/database.py creates three
# PLAIN B-tree indexes, not partial ones.
#
# What is actually true: this query is dominated by the pgvector cosine scan
# (`embedding <=> ...`), for which there is no ANN index at all yet — see the
# "add an HNSW index once chunk counts grow" note in app/database.py. The WHERE
# clause is therefore not the cost driver at present volumes, and the toggle
# rewrite cannot regress a plan that was already a sequential scan.
#
# If chunk volume grows enough for this to matter, verify with a real EXPLAIN
# (ANALYZE, BUFFERS) against Postgres rather than reasoning about it. Generic
# plans for parameterised booleans are NOT guaranteed to prune branches; that
# depends on Postgres choosing a custom plan. Do not restore the old claim
# without that measurement in hand.
#
# `doc_category` values ('kb', 'change_doc', 'code') stay as inline literals:
# they are a closed vocabulary defined in this codebase, never user input, and
# keeping them inline lets the planner see the exact constant.
#
# Do NOT convert this back to per-call assembly. `backend/tests/test_no_dynamic_sql.py`
# fails the build if any f-string or concatenation reaches a SQL executor.
_RETRIEVE_SQL = _sql(
    "SELECT content, doc_category, source_key, metadata::text, "
    "(embedding <=> CAST(:q AS vector)) AS dist "
    "FROM document_chunks "
    "WHERE ("
    "  (:want_change AND doc_category = 'change_doc' AND change_id = :cid)"
    "  OR (:want_kb AND doc_category = 'kb')"
    "  OR (:want_code AND doc_category = 'code' AND repo_id = :rid)"
    ") "
    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
)


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

    # Which scopes this call actually wants. Computed in Python, then passed to
    # the static statement below as BOUND BOOLEANS — see _RETRIEVE_SQL for why
    # the SQL itself is no longer assembled from these.
    want_change = ("change_doc" in categories) and bool(change_id)
    want_kb = "kb" in categories
    want_code = ("code" in categories) and bool(repo_id)

    # Nothing in scope — no query to run. Preserved from the original: without
    # this, an all-false predicate set would scan the table to return nothing.
    if not (want_change or want_kb or want_code):
        return []

    params: dict = {
        "q": vector_literal(qvec),
        "k": int(top_k),
        "want_change": want_change,
        "want_kb": want_kb,
        "want_code": want_code,
        # Bound unconditionally, NULL when the corresponding scope is off. Safe
        # because the matching `want_*` flag is then false, and in SQL
        # `false AND (col = NULL)` is false — the branch contributes no rows.
        "cid": change_id if want_change else None,
        "rid": repo_id if want_code else None,
    }

    try:
        rows = db.execute(_RETRIEVE_SQL, params).fetchall()
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
