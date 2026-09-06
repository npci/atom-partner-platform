# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

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


# ── Fully-static DELETE statements (sonatype-2021-0025 hardening) ───────────
#
# WHY THESE ARE CONSTANTS AND NOT AN f-STRING
#
# This function used to build its WHERE clause by joining a list and
# interpolating it:
#
#     db.execute(_sql(f"DELETE FROM document_chunks WHERE {' AND '.join(clauses)}"), params)
#
# That code was SAFE at runtime — `clauses` only ever held hardcoded predicate
# literals and every value went through a bound parameter. But it is not
# safe-by-CONSTRUCTION, and that distinction is the whole finding:
#
#   1. To a taint analyser (and to Sonatype's sonatype-2021-0025 advisory, which
#      flags dynamic SQL assembly on SQLAlchemy), an f-string reaching `text()`
#      is a SQL-injection sink. It cannot prove the interpolated list holds only
#      literals, so it reports the path. The advisory has no fixed version, so
#      this pattern would keep firing on every scan forever regardless of which
#      SQLAlchemy version we pin.
#   2. More importantly, the old shape was one careless edit away from being a
#      REAL injection. Appending `clauses.append(f"x = '{user_value}'")` would
#      have compiled, passed review as "matching the existing style", and shipped
#      an exploitable query. The safety lived in a convention, not in the code.
#
# Enumerating the call shapes as module-level constants removes the sink
# entirely: every SQL string below is a literal fixed at import time, so there is
# no construction step for an analyser to flag or a future edit to poison. The
# combinations are closed and small (this table has exactly three optional scope
# columns), so exhaustive enumeration costs a few lines and buys a provable
# property.
#
# ── THE ENUMERATION MUST BE COMPLETE: 2^3 = 8, NOT 6 ────────────────────────
# An earlier revision of this table defined only SIX constants and selected them
# with an if/elif chain that tested `repo_id` before `change_id`. When a caller
# passed BOTH, the chain took the repo branch and SILENTLY DROPPED the
# `change_id = :cid` predicate — issuing a DELETE strictly BROADER than the
# caller asked for. Verified: with both set, six extra rows were removed.
#
# It was latent (today's two call sites pass one or the other) but it was a real
# data-loss bug one caller away, and it made the docstring's "predicates are
# exactly those whose argument is not None" claim false. The lesson is that a
# partial enumeration reached by branch priority is strictly worse than the
# f-string it replaced: the f-string at least always produced the correct
# predicate set.
#
# So all eight combinations are now present and selected by an exact key rather
# than by branch order, which cannot silently degrade. `test_no_dynamic_sql.py`
# asserts the table is complete, and `test_delete_chunks_scoping.py` asserts the
# executed predicate set matches the requested scope for every combination.
#
# If a fourth scope is ever needed, this table doubles to 16 — at that point
# switch to a SQLAlchemy Core `delete()` construct with `.where()` clauses, which
# is composable AND still parameterised (it builds no SQL string). Do NOT
# reintroduce text-mode string building.
_DEL_BY_CATEGORY = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat"
)
_DEL_BY_CATEGORY_CHANGE = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat AND change_id = :cid"
)
_DEL_BY_CATEGORY_SOURCE = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat AND source_key = :sk"
)
_DEL_BY_CATEGORY_CHANGE_SOURCE = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat "
    "AND change_id = :cid AND source_key = :sk"
)
_DEL_BY_CATEGORY_REPO = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat AND repo_id = :rid"
)
_DEL_BY_CATEGORY_REPO_SOURCE = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat "
    "AND repo_id = :rid AND source_key = :sk"
)
# ── The two combinations the earlier revision was missing ───────────────────
_DEL_BY_CATEGORY_CHANGE_REPO = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat "
    "AND change_id = :cid AND repo_id = :rid"
)
_DEL_BY_CATEGORY_CHANGE_REPO_SOURCE = _sql(
    "DELETE FROM document_chunks WHERE doc_category = :cat "
    "AND change_id = :cid AND repo_id = :rid AND source_key = :sk"
)

# Exact-match dispatch table, keyed by which scopes are present as
# (has_change_id, has_repo_id, has_source_key).
#
# A dict keyed on the full tuple is used rather than an if/elif chain BECAUSE of
# the bug described above: a chain encodes a priority order, so an unhandled
# combination silently falls through to whichever branch happens to match first.
# A dict lookup has no fallback — a missing key raises KeyError immediately
# instead of quietly running a broader DELETE. The completeness test asserts all
# eight keys are present, so that KeyError is unreachable in practice.
_DELETE_STATEMENTS = {
    # (change_id, repo_id, source_key)
    (False, False, False): _DEL_BY_CATEGORY,
    (False, False, True): _DEL_BY_CATEGORY_SOURCE,
    (False, True, False): _DEL_BY_CATEGORY_REPO,
    (False, True, True): _DEL_BY_CATEGORY_REPO_SOURCE,
    (True, False, False): _DEL_BY_CATEGORY_CHANGE,
    (True, False, True): _DEL_BY_CATEGORY_CHANGE_SOURCE,
    (True, True, False): _DEL_BY_CATEGORY_CHANGE_REPO,
    (True, True, True): _DEL_BY_CATEGORY_CHANGE_REPO_SOURCE,
}


def _delete_chunks(db: Session, *, doc_category: str, change_id: str | None = None,
                   source_key: str | None = None, repo_id: str | None = None) -> None:
    """Delete the chunks for one ingestion scope.

    Selects a pre-built static statement from `_DELETE_STATEMENTS` rather than
    assembling SQL. Values are bound, never interpolated.

    Scoping contract, which `test_delete_chunks_scoping.py` enforces for all
    eight combinations: the predicates applied are EXACTLY `doc_category` plus
    one for each of `change_id`, `repo_id` and `source_key` that is not None,
    ANDed together. Passing more arguments always narrows the delete and never
    widens it. This matches the pre-hardening dynamic implementation exactly.
    """
    params: dict = {"cat": doc_category}

    # Build the dispatch key from which scopes were supplied, then bind only the
    # matching values. Every combination has its own statement, so no argument
    # can be silently ignored — contrast the earlier if/elif version, which
    # dropped `change_id` whenever `repo_id` was also given.
    if change_id is not None:
        params["cid"] = change_id
    if repo_id is not None:
        params["rid"] = repo_id
    if source_key is not None:
        params["sk"] = source_key

    stmt = _DELETE_STATEMENTS[
        (change_id is not None, repo_id is not None, source_key is not None)
    ]
    db.execute(stmt, params)


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
