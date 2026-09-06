# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Embeddings via the partner-side Ollama (nomic-embed-text, 768-dim).

Mirrors NPCI `backend/app/rag/embeddings.py` but talks to the partner's own
Ollama service. Returns 768-dim vectors that land in pgvector `vector(768)`
columns.

Fix-on-copy (uat review finding H2): the batch path asserts the response length
matches the batch and falls back to per-item embedding on any mismatch, so a
short/garbled Ollama response can never silently misalign vectors. The embedding
model id is part of the cache key on the embed_cache side, so a model change
never serves stale vectors.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def vector_literal(vec: list[float]) -> str:
    """pgvector text form `[f1,f2,...]` for binding into `CAST(:v AS vector)`."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def is_zero(vec: list[float]) -> bool:
    """True for a hard-failed (all-zero) embedding — callers must not store/use it."""
    return not any(vec)


def _call_breaker_guarded(fn, *, what: str) -> tuple[object, bool]:
    """Run `fn()` inside the `ollama_embed` circuit breaker.

    Returns `(result, failed)`. `failed=True` covers all three cases the
    callers already treat identically — the circuit is open, the call raised,
    or resilience itself is misconfigured — so neither caller has to learn a
    new failure mode. Nothing propagates out of here: this boundary is H2 and
    its documented behaviour is to degrade (fall back per-item, or return a
    zero vector), never to abort the indexing run.

    A trip is logged at WARNING with the boundary name so the operator sees
    "Ollama is down, embeddings are degraded" instead of a silent quality drop.
    """
    from app.core.resilience import CircuitOpenError, breaker_for

    try:
        breaker = breaker_for("ollama_embed")
    except Exception:  # noqa: BLE001 — misconfigured registry must not stop indexing
        logger.warning("ollama_embed breaker unavailable; proceeding unguarded", exc_info=True)
        try:
            return fn(), False
        except Exception:  # noqa: BLE001
            logger.warning("%s failed", what, exc_info=True)
            return None, True

    try:
        with breaker.call():
            return fn(), False
    except CircuitOpenError:
        logger.warning(
            "%s skipped — ollama_embed circuit is OPEN (Ollama looks down; "
            "embeddings degrade to zero vectors until it recovers)", what,
        )
        return None, True
    except Exception:  # noqa: BLE001 — counted by the breaker, then softened here
        logger.warning("%s failed", what, exc_info=True)
        return None, True


def _embed_batch(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch via Ollama /api/embed. Returns a list aligned 1:1 with
    `texts`, or None on any failure / length mismatch (caller falls back).

    Wrapped in the `ollama_embed` circuit breaker (EA_Skills.md P7/P8;
    security_architecture_skills.md §11.3). The boundary's limits have always
    been declared in `core/hostility.py` — H2, threshold 8, 15s cooldown — but
    nothing enforced them, so a down Ollama was retried on every single batch
    and every per-item fallback, each paying the full 120s read timeout. One
    indexing run over a large repo could therefore hang for hours.

    `_call_breaker_guarded` keeps this path's existing contract intact: an open
    circuit returns None (the caller's documented "fall back" signal) rather
    than raising, so a breaker trip degrades indexing quality exactly the way a
    transport failure already did — it never turns a soft failure into a hard
    one.
    """
    if not texts:
        return []
    url = f"{settings.ollama_url.rstrip('/')}/api/embed"

    def _post():
        resp = httpx.post(
            url,
            # keep_alive=-1 pins the model in memory — the default ~5min idle
            # unload made the first embed of a run pay a multi-minute CPU
            # cold-load (observed live: ~2-3min of silence before a code plan).
            json={"model": settings.embed_model, "input": texts, "keep_alive": -1},
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json().get("embeddings")

    embs, failed = _call_breaker_guarded(_post, what=f"embed batch ({len(texts)} texts)")
    if failed:
        return None
    # H2 guard — never trust the count.
    if not isinstance(embs, list) or len(embs) != len(texts):
        logger.warning(
            "embed batch length mismatch: sent=%d got=%s — falling back per-item",
            len(texts), (len(embs) if isinstance(embs, list) else type(embs).__name__),
        )
        return None
    return embs


def _embed_one(text: str) -> list[float]:
    """Embed a single text via Ollama /api/embeddings. Returns a zero vector on
    failure (callers/embed_cache must treat all-zero as a non-cacheable miss).

    Shares the `ollama_embed` breaker with `_embed_batch` — deliberately one
    breaker for the boundary, not one per endpoint. Both hit the same Ollama
    process, so a failure on either is evidence about the same dependency, and
    the per-item path is precisely the fallback a failing batch drops into.
    Separate breakers would let the fallback hammer a service the batch path
    had already given up on.
    """
    url = f"{settings.ollama_url.rstrip('/')}/api/embeddings"

    def _post():
        resp = httpx.post(
            url,
            json={"model": settings.embed_model, "prompt": text, "keep_alive": -1},
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json().get("embedding")

    emb, failed = _call_breaker_guarded(_post, what=f"embed_one ({len(text)} chars)")
    if not failed:
        if isinstance(emb, list) and len(emb) == settings.embed_dim:
            return emb
        logger.warning("embed_one: unexpected embedding shape for a %d-char text", len(text))
    return [0.0] * settings.embed_dim


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed `texts` → 768-dim vectors, aligned 1:1 with the input.

    Batches via /api/embed; on any batch failure/mismatch, falls back to
    per-item /api/embeddings so the alignment is always exact. A failed item
    yields a zero vector (callers should skip caching all-zero vectors)."""
    out: list[list[float]] = []
    bs = max(1, settings.embed_batch_size)
    for i in range(0, len(texts), bs):
        batch = texts[i:i + bs]
        embs = _embed_batch(batch)
        if embs is None:
            embs = [_embed_one(t) for t in batch]
        out.extend(embs)
    return out


def warm_up(timeout_sec: float = 10.0) -> None:
    """Fire one cheap embed so Ollama loads the model into memory before the
    first real request. Best-effort — failures are swallowed."""
    url = f"{settings.ollama_url.rstrip('/')}/api/embeddings"
    try:
        httpx.post(
            url,
            json={"model": settings.embed_model, "prompt": "warmup"},
            timeout=timeout_sec,
        )
        logger.info("embed warmup ok (model=%s)", settings.embed_model)
    except Exception as e:  # noqa: BLE001
        logger.warning("embed warmup failed (non-fatal)", exc_info=True)
