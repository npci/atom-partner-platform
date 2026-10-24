"""Version-aware change context for the partner analyser + draft suggester.

Instead of dumping every received version of every document into the prompt,
this assembles:
  - the **v1 baseline** documents (the original spec the partner first received), and
  - an LLM-generated **summary of what changed** in later versions (option C).

A revision whose content is byte-identical to v1 (today's placeholder carry-forward)
skips the LLM entirely and gets a one-line note, so the prompt stays clean until the
real regeneration pipeline produces actual differences. PARTNER.md remains the
capability profile inside the consuming agents — this only shapes the change docs.
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.llm import call_llm
from app.models import ChangeDocument

logger = logging.getLogger(__name__)

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
