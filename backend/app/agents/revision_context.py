# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Version-aware change context for the partner analyser + draft suggester.

Instead of dumping every received version of every document into the prompt,
this assembles:
  - the **v1 baseline** documents (the original spec the partner first received), and
  - an LLM-generated **summary of what changed** in later versions (option C).

A revision whose content is byte-identical to v1 (today's placeholder carry-forward)
skips the LLM entirely and gets a one-line note, so the prompt stays clean until the
real regeneration pipeline produces actual differences. PARTNER.md remains the
capability profile inside the consuming agents — this only shapes the change docs.

Caching (Finding 8: security_architecture_skills.md §5.5, EA_Skills.md P6/P2):
the design -> code -> codegen -> review -> fix pipeline for a single change
calls this function independently from each phase's job runner, each time
re-fetching every ChangeDocument row and, on a revised change, re-running the
`_summarize_changes` LLM call. `assemble_change_context()` now caches its
result per change_id, invalidated by a cheap fingerprint (row count + max
negotiation_version) rather than a blind TTL — a kit revision arriving
mid-pipeline is picked up on the VERY NEXT call, not after some fixed window
expires, while a same-version re-call within the pipeline's few-minute
lifetime is served from cache with no DB content fetch and no LLM call.
"""
import logging
import time
from collections import OrderedDict
from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.llm import call_llm
from app.models import ChangeDocument

logger = logging.getLogger(__name__)

# {change_id: (fingerprint, cached_at_monotonic, result)}
#
# BOUNDED, LRU-evicted (EA_Skills.md P2 "Mechanical Sympathy and Shared-Nothing
# Concurrency" — "Flag when: unbounded queues are used"; "Recommend: bounded
# queues/ring buffers"). An unbounded dict here would be a slow memory leak: one
# entry per change_id, each holding the FULL assembled document set for that
# change, retained for the process's lifetime. A long-running partner deployment
# processing thousands of changes would accumulate all of them in RAM, and the
# TTL below does not help — expired entries are only skipped on read, never
# reclaimed, so a change that is never revisited is never evicted.
#
# `OrderedDict` + `move_to_end` on hit gives true LRU: the entry evicted at
# capacity is the least recently *used*, not merely the oldest inserted.
_CONTEXT_CACHE: "OrderedDict[str, tuple[tuple, float, dict]]" = OrderedDict()
_CONTEXT_CACHE_LOCK = Lock()
# Belt-and-suspenders TTL on top of the fingerprint check — bounds staleness
# even in the (currently impossible, since ChangeDocument rows are append-only
# and never edited in place) case of a same-row content mutation that wouldn't
# change the fingerprint.
_CONTEXT_CACHE_TTL_S = 300  # 5 minutes


def _fingerprint(db: Session, change_id: str) -> tuple:
    """Cheap (COUNT + MAX, no content fetched) signature of this change's
    document set. Changes the moment a new ChangeDocument row lands (a new
    doc_type, OR a kit revision bumping negotiation_version) — the cache
    invalidates on the very next call rather than waiting out a TTL."""
    row = db.execute(
        select(func.count(ChangeDocument.id), func.max(ChangeDocument.negotiation_version))
        .where(ChangeDocument.change_id == change_id)
    ).one()
    return (row[0] or 0, row[1] or 0)


# Per-side cap when feeding v1 + vN into the diff-summary prompt.
_MAX_DIFF_DOC_CHARS = 9000

_SUMMARY_SYSTEM = (
    "You compare two versions of NPCI change documents and summarise WHAT CHANGED, "
    "concisely, for a bank's implementation team. Output plain-text bullet points "
    "grouped by document heading. Focus only on substantive specification changes "
    "(scope, limits/thresholds, timelines, API/schema contracts, dependencies, "
    "certification role). Ignore pure formatting/wording. Be specific and factual; "
    "never invent values, dates, or sections not present in the text."
)


def assemble_change_context(db: Session, change_id: str, api_key: str | None = None) -> dict:
    """Return {documents, revision_summary, current_version} — cached per
    change_id, invalidated by a fingerprint of the document set (see module
    docstring). Cache hits skip both the DB row fetch AND, for a revised
    change, the `_summarize_changes` LLM call entirely."""
    fp = _fingerprint(db, change_id)
    now = time.monotonic()
    with _CONTEXT_CACHE_LOCK:
        cached = _CONTEXT_CACHE.get(change_id)
        if cached and cached[0] == fp and (now - cached[1]) < _CONTEXT_CACHE_TTL_S:
            # Mark as most-recently-used so a hot change isn't evicted just
            # because it was inserted long ago.
            _CONTEXT_CACHE.move_to_end(change_id)
            return cached[2]

    result = _assemble_change_context_uncached(db, change_id, api_key)

    with _CONTEXT_CACHE_LOCK:
        _CONTEXT_CACHE[change_id] = (fp, now, result)
        _CONTEXT_CACHE.move_to_end(change_id)
        # Evict least-recently-used entries past the cap. `while` rather than a
        # single pop so a lowered max_entries setting converges immediately
        # instead of leaking one entry per insert until it catches up.
        max_entries = _max_entries()
        while len(_CONTEXT_CACHE) > max_entries:
            evicted_id, _ = _CONTEXT_CACHE.popitem(last=False)
            logger.debug(
                "revision_context cache evicted %s (LRU, size cap %d)",
                evicted_id, max_entries,
            )
    return result


def _max_entries() -> int:
    """Cache capacity, read at call time so a test/operator override of
    `settings.context_cache_max_entries` takes effect without a restart.
    Floored at 1 — a zero/negative cap would evict every entry immediately
    after inserting it, silently disabling the cache while still paying the
    full assembly cost on every call."""
    from app.config import settings
    return max(1, int(getattr(settings, "context_cache_max_entries", 128)))


def context_cache_stats() -> dict:
    """Observability hook (EA_Skills.md P9) — current size and capacity, so
    saturation is measurable rather than guessed at."""
    with _CONTEXT_CACHE_LOCK:
        return {"entries": len(_CONTEXT_CACHE), "max_entries": _max_entries()}


def invalidate_context_cache(change_id: str | None = None) -> None:
    """Drop the cached context for `change_id`, or the entire cache when
    `change_id` is None. Not required for correctness (the fingerprint check
    already invalidates on a new/changed document row) — provided as an
    explicit escape hatch for callers/tests that want a guaranteed-fresh
    assembly without waiting on the fingerprint or TTL."""
    with _CONTEXT_CACHE_LOCK:
        if change_id is None:
            _CONTEXT_CACHE.clear()
        else:
            _CONTEXT_CACHE.pop(change_id, None)


def _assemble_change_context_uncached(db: Session, change_id: str, api_key: str | None = None) -> dict:
    """Return {documents, revision_summary, current_version}.

    `documents` is the v1 baseline (one {doc_type, content} per doc_type).
    `revision_summary` is an LLM summary of later-version changes, a short note
    when versions were carried forward unchanged, or None when only v1 exists.
    """
    rows = db.scalars(
        select(ChangeDocument).where(ChangeDocument.change_id == change_id)
    ).all()
    if not rows:
        return {"documents": [], "revision_summary": None, "current_version": 1}

    by_type: dict[str, list[ChangeDocument]] = {}
    for r in rows:
        by_type.setdefault(r.doc_type, []).append(r)

    baseline_docs: list[dict] = []
    changed: list[dict] = []
    current_version = 1
    for doc_type, versions in by_type.items():
        versions.sort(key=lambda r: (r.negotiation_version or 1))
        base, latest = versions[0], versions[-1]
        current_version = max(current_version, latest.negotiation_version or 1)
        baseline_docs.append({"doc_type": doc_type, "content": base.content or ""})
        if (latest.negotiation_version or 1) > (base.negotiation_version or 1) \
                and (latest.content or "") != (base.content or ""):
            changed.append({
                "doc_type": doc_type,
                "v1": base.content or "",
                "latest": latest.content or "",
                "latest_version": latest.negotiation_version or 1,
            })

    if current_version <= 1:
        return {"documents": baseline_docs, "revision_summary": None, "current_version": 1}

    if not changed:
        return {
            "documents": baseline_docs,
            "revision_summary": (
                f"NPCI published revisions up to v{current_version}, but the documents "
                "were carried forward unchanged (no content changes versus v1)."
            ),
            "current_version": current_version,
        }

    return {
        "documents": baseline_docs,
        "revision_summary": _summarize_changes(changed, current_version, api_key),
        "current_version": current_version,
    }


def _summarize_changes(changed: list[dict], current_version: int, api_key: str | None) -> str:
    """LLM pre-pass: summarise v1 → latest differences for the changed docs."""
    parts = [
        f"Summarise what changed from v1 to v{current_version} for each document below. "
        "Group bullets under each document heading.\n",
    ]
    for c in changed:
        parts.append(
            f"## {c['doc_type']} (v1 → v{c['latest_version']})\n"
            f"--- v1 ---\n{c['v1'][:_MAX_DIFF_DOC_CHARS]}\n"
            f"--- v{c['latest_version']} ---\n{c['latest'][:_MAX_DIFF_DOC_CHARS]}\n"
        )
    try:
        text = call_llm(
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(parts)}],
            max_tokens=1500,
            api_key=api_key,
        )
        summary = (text or "").strip()
        if summary:
            return summary
    except Exception as exc:  # noqa: BLE001 — degrade, never block the analysis
        logger.warning("revision summary LLM call failed", exc_info=True)

    names = ", ".join(c["doc_type"] for c in changed)
    return (
        f"NPCI revised the kit up to v{current_version}. Changed documents: {names}. "
        "(Automatic change-summary unavailable; review the documents directly.)"
    )
