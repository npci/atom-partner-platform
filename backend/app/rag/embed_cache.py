# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Embedding cache over pgvector (`embedding_cache` on partner_postgres).

Keyed on (content_sha256, embedding_model) so a model change never serves a stale
vector. Fail-soft: a missing/broken cache table degrades to a plain miss, never
breaks ingestion. All-zero vectors (hard embed failures, see embeddings._embed_one)
are NOT cached, so a transient failure isn't frozen as a permanent dead entry.

Raw SQL (psycopg + pgvector) rather than the SQLAlchemy pgvector type — matches
NPCI's approach and keeps the dependency surface to the Postgres extension only.
Vectors are passed as their text form `[f1,f2,...]` and cast to `vector`.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import text as _sql

from app.config import settings
from app.database import engine
from app.rag.embeddings import embed_texts

logger = logging.getLogger(__name__)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _is_zero(vec: list[float]) -> bool:
    return not any(vec)


def _get_cached(hashes: list[str]) -> dict[str, list[float]]:
    """sha256 → vector for the hashes already cached for the current model."""
    if not hashes:
        return {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                _sql(
                    "SELECT content_sha256, embedding::text "
                    "FROM embedding_cache "
                    "WHERE model = :m AND content_sha256 = ANY(:hashes)"
                ),
                {"m": settings.embed_model, "hashes": hashes},
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — fail-soft to a miss
        logger.warning("embed_cache get failed (degrading to miss)", exc_info=True)
        return {}
    out: dict[str, list[float]] = {}
    for sha, emb_text in rows:
        try:
            out[sha] = [float(x) for x in emb_text.strip()[1:-1].split(",") if x]
        except (ValueError, AttributeError):
            continue
    return out


def _put_cached(pairs: list[tuple[str, list[float]]]) -> None:
    """Insert (sha256, vector) pairs, skipping all-zero vectors. Idempotent via
    ON CONFLICT DO NOTHING so concurrent workers don't fight."""
    rows = [(sha, _vec_literal(v)) for sha, v in pairs if not _is_zero(v)]
    if not rows:
        return
    try:
        with engine.begin() as conn:
            for sha, vlit in rows:
                conn.execute(
                    _sql(
                        "INSERT INTO embedding_cache (content_sha256, model, embedding) "
                        "VALUES (:s, :m, CAST(:e AS vector)) "
                        "ON CONFLICT (content_sha256, model) DO NOTHING"
                    ),
                    {"s": sha, "m": settings.embed_model, "e": vlit},
                )
    except Exception as e:  # noqa: BLE001 — fail-soft; a missed write just re-embeds next time
        logger.warning("embed_cache put failed (non-fatal)", exc_info=True)


def embed_texts_cached(texts: list[str]) -> list[list[float]]:
    """Embed `texts` with the cache in front. Returns vectors aligned 1:1.

    Cache hits skip the model entirely; misses are embedded once and written back
    (non-zero only). Same content within one call is de-duplicated."""
    if not texts:
        return []
    hashes = [_sha(t) for t in texts]
    cached = _get_cached(list(set(hashes)))

    # Embed the distinct misses once.
    miss_order: list[str] = []
    seen: set[str] = set()
    for h, t in zip(hashes, texts):
        if h not in cached and h not in seen:
            seen.add(h)
            miss_order.append(t)
    if miss_order:
        new_vecs = embed_texts(miss_order)
        new_by_sha = {_sha(t): v for t, v in zip(miss_order, new_vecs)}
        _put_cached(list(new_by_sha.items()))
        cached.update(new_by_sha)

    return [cached.get(h, [0.0] * settings.embed_dim) for h in hashes]
