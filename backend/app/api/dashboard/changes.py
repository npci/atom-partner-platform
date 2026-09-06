# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: changes — the inbox, change detail, and document downloads."""
import logging
import re
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import ChangeDocument, IncomingChange, OutgoingQuery, PartnerUser, ProgressReport

from ._shared import _doc_title, markdown_to_docx_bytes, markdown_to_pptx_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/changes")
def list_changes(user: PartnerUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """Inbox list. Enriched with the per-change signals the dashboard
    needs to render KPI cards and rich rollout cards without an N+1
    follow-up: decision, cert_status, and a doc count derived from
    `change_documents`. Keep this lightweight — one SELECT plus one
    aggregate count per row (cheap on SQLite, N changes is small).
    """
    from sqlalchemy import func
    changes = db.scalars(select(IncomingChange).order_by(IncomingChange.received_at.desc())).all()
    # Batch the doc counts so we don't fire N queries in a loop.
    if changes:
        doc_counts = dict(
            db.execute(
                select(ChangeDocument.change_id, func.count(ChangeDocument.id))
                .where(ChangeDocument.change_id.in_([c.id for c in changes]))
                .group_by(ChangeDocument.change_id)
            ).all()
        )
    else:
        doc_counts = {}
    return [
        {
            "id":             c.id,
            "npci_change_id": c.npci_change_id,
            "title":          c.title,
            "status":         c.status,
            "decision":       c.decision or "pending",
            "cert_status":    c.cert_status,
            "received_at":    c.received_at.isoformat() if c.received_at else None,
            "documents_count": doc_counts.get(c.id, 0),
        }
        for c in changes
    ]


@router.get("/changes/{change_id}")
def get_change(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    # ProductKit revisions append doc rows tagged with the published version.
    # Return ALL versions, ordered so the UI can group by doc_type and offer a
    # per-document version switch (v1 / v2 / …); newest version last per type.
    docs = db.scalars(
        select(ChangeDocument)
        .where(ChangeDocument.change_id == change_id)
        .order_by(ChangeDocument.doc_type, ChangeDocument.negotiation_version)
    ).all()
    available_versions = sorted({(d.negotiation_version or 1) for d in docs})
    queries = db.scalars(
        select(OutgoingQuery).where(OutgoingQuery.change_id == change_id).order_by(OutgoingQuery.sent_at)
    ).all()
    progress = db.scalars(
        select(ProgressReport).where(ProgressReport.change_id == change_id).order_by(ProgressReport.reported_at)
    ).all()

    import json
    def _safe_json(s):
        if not s:
            return None
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return None

    return {
        "id": change.id,
        "npci_change_id": change.npci_change_id,
        "title": change.title,
        "initial_prompt": change.initial_prompt,
        "enhanced_prompt": change.enhanced_prompt,
        "status": change.status,
        "decision": change.decision,
        # Cert lifecycle — surfaced here so the LifecycleStepper can
        # light up the "Certified" dot once the orchestrator has flipped
        # cert_status to 'certified' (driven by an all-PASS cert_test_response
        # in partner_handlers.handle_cert_test_response).
        "cert_status":         change.cert_status,
        "cert_status_history": _safe_json(change.cert_status_history) or {},
        "cert_summary":        _safe_json(change.cert_summary),
        "npci_counter": _safe_json(change.npci_counter),
        "npci_counter_history": _safe_json(change.npci_counter_history) or [],
        "counter_decisions": _safe_json(change.counter_decisions) or [],
        # Follow-up NPCI replies to already-answered general questions —
        # rendered as extra NPCI bubbles so a second reply doesn't
        # overwrite the first in the partner timeline.
        "npci_followups": _safe_json(change.npci_followups) or [],
        # NPCI round-lifecycle notices (PM force-closed a round) — shown
        # in the Negotiation tab so the partner sees the window shut.
        "round_notices": _safe_json(change.round_notices) or [],
        "blockers": _safe_json(change.blockers) or [],
        # Post-freeze emergency issues raised to NPCI (Slice 5). The button
        # that creates these is gated on negotiation_version >= 3 in the UI.
        "emergency_issues": _safe_json(getattr(change, "emergency_issues", None)) or [],
        "received_at": change.received_at.isoformat() if change.received_at else None,
        # Published-version state drives the "New version available" banner and
        # the document version switcher. Without these the banner never renders.
        "negotiation_version":          change.negotiation_version,
        "negotiation_version_accepted": change.negotiation_version_accepted,
        # Set when NPCI freezes the negotiation (round cap reached). Drives the
        # partner-side frozen UI — a round-based freeze may not bump the version.
        "negotiation_finalized_at":     getattr(change, "negotiation_finalized_at", None),
        "npci_change_summary":          getattr(change, "npci_change_summary", None),
        # Query hold — true while NPCI prepares a revised kit (round closed →
        # ship). The UI freezes the composer and shows a hold banner.
        "revision_in_progress":         bool(getattr(change, "revision_in_progress", False)),
        "revision_target_version":      getattr(change, "revision_target_version", None),
        "available_versions":           available_versions,
        "documents": [
            {
                "id":            d.id,
                "doc_type":      d.doc_type,
                "content":       d.content,
                "version":       d.version,
                "negotiation_version": d.negotiation_version or 1,
                # Surface format availability so the UI shows the right
                # download buttons. .docx is always available — we
                # synthesise it from the markdown content if NPCI didn't
                # ship bytes. .pptx is only meaningful for product_deck
                # (matching NPCI's behaviour).
                "has_docx":      True if (d.docx_bytes or d.content) else False,
                "docx_filename": d.docx_filename,
                "has_pptx":      bool(d.pptx_bytes) or d.doc_type == "product_deck",
                "pptx_filename": d.pptx_filename,
                # .xlsx is only meaningful for cert_test_cases and is
                # only servable when NPCI shipped the exact workbook
                # bytes over A2A (excel_testcase_engine output).
                "has_xlsx":      bool(d.xlsx_bytes),
                "xlsx_filename": d.xlsx_filename,
                # .mp4 promo/explainer video — only when NPCI shipped the bytes.
                "has_video":     bool(d.video_bytes),
                "video_filename": d.video_filename,
                # .zip multi-schema XSD bundle — only when NPCI shipped a change
                # touching ≥2 .xsd files. Drives the "Download all schemas" button.
                "has_zip":       bool(d.zip_bytes),
                "zip_filename":  d.zip_filename,
            }
            for d in docs
        ],
        "queries": [
            {
                "id": q.id, "message": q.message, "response": q.response, "status": q.status,
                # kind is 'general' (clarifying question) or 'negotiation'
                # (counter-propose justification sent via /counter). The
                # partner UI's events-builder routes 'negotiation' to the
                # Negotiation tab instead of General.
                "kind": q.kind,
                "sent_at": q.sent_at.isoformat() if q.sent_at else None,
                "response_received_at": q.response_received_at.isoformat() if q.response_received_at else None,
            }
            for q in queries
        ],
        "progress": [
            {"step": p.step, "reported_at": p.reported_at.isoformat() if p.reported_at else None}
            for p in progress
        ],
    }


@router.get("/changes/{change_id}/versions")
def get_change_versions(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Published kit versions this partner has received for the change."""
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    from sqlalchemy import func
    versions = sorted(
        v for (v,) in db.execute(
            select(func.distinct(ChangeDocument.negotiation_version))
            .where(ChangeDocument.change_id == change_id)
        ).all()
        if v is not None
    )
    return {
        "change_id": change_id,
        "negotiation_version": change.negotiation_version,
        "negotiation_version_accepted": change.negotiation_version_accepted,
        "available_versions": versions or [1],
    }


@router.get("/changes/{change_id}/documents/{doc_id}/download")
def download_change_document(
    change_id: str,
    doc_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream the `.docx` rendition NPCI sent over A2A back to the
    operator's browser.

    Bytes are stored in `change_documents.docx_bytes` (BLOB). 404 if the
    doc has no docx attachment. Filename comes from the column populated
    at ingest time; falls back to a synthetic `<doc_type>_v<n>.docx` if
    NPCI omitted it.
    """
    doc = db.get(ChangeDocument, doc_id)
    if not doc or doc.change_id != change_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Prefer the binary NPCI shipped over A2A. Fall back to on-the-fly
    # synthesis from the stored markdown content so every doc is
    # downloadable in .docx, matching what the NPCI portal advertises.
    if doc.docx_bytes:
        body  = doc.docx_bytes
        fname = doc.docx_filename or f"{doc.doc_type}_v{doc.version}.docx"
    else:
        if not doc.content:
            raise HTTPException(status_code=404, detail="Document has no content to render")
        body  = markdown_to_docx_bytes(doc.content, title=_doc_title(doc))
        fname = f"{doc.doc_type}_v{doc.version}.docx"

    return Response(
        content=body,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{urlquote(fname)}"',
        },
    )


@router.get("/changes/{change_id}/documents/{doc_id}/video")
def get_change_document_video(
    change_id: str,
    doc_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream the promo/explainer `.mp4` NPCI shipped with the kit (inline, so
    the UI can play it in a <video> tag and offer download). 404 if none."""
    doc = db.get(ChangeDocument, doc_id)
    if not doc or doc.change_id != change_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.video_bytes:
        raise HTTPException(status_code=404, detail="Document has no video attachment")
    fname = doc.video_filename or f"{doc.doc_type}_v{doc.version}.mp4"
    return Response(
        content=doc.video_bytes,
        media_type="video/mp4",
        headers={"Content-Disposition": f'inline; filename="{urlquote(fname)}"'},
    )


@router.get("/changes/{change_id}/change-summary.docx")
def download_change_summary(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream NPCI's "Summary of Changes" for this kit version as a .docx."""
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    summary = (getattr(change, "npci_change_summary", None) or "").strip()
    if not summary:
        raise HTTPException(status_code=404, detail="No change summary for this change")
    ver = change.negotiation_version or 1
    body = markdown_to_docx_bytes(summary, title=f"Summary of Changes — v{ver}")
    fname = f"change_summary_v{ver}.docx"
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{urlquote(fname)}"'},
    )


@router.get("/changes/{change_id}/documents/{doc_id}/download/pptx")
def download_change_document_pptx(
    change_id: str,
    doc_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream the `.pptx` Product Deck companion (D7).

    Mirror of the .docx endpoint above. Bytes live in
    `change_documents.pptx_bytes`; 404 if the doc has no pptx
    attachment (every doc except product_deck, plus product_decks
    where the LLM didn't emit a valid JSON outline at gen time).
    """
    doc = db.get(ChangeDocument, doc_id)
    if not doc or doc.change_id != change_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # .pptx is only meaningful for deck-style doc types. Match NPCI's
    # behaviour: shipped only for `product_deck`. Other types get a 404.
    if doc.doc_type != "product_deck" and not doc.pptx_bytes:
        raise HTTPException(status_code=404, detail="No .pptx for this document type")

    if doc.pptx_bytes:
        body  = doc.pptx_bytes
        fname = doc.pptx_filename or f"{doc.doc_type}_v{doc.version}.pptx"
    else:
        if not doc.content:
            raise HTTPException(status_code=404, detail="Deck content unavailable")
        body  = markdown_to_pptx_bytes(doc.content, title=_doc_title(doc))
        fname = f"{doc.doc_type}_v{doc.version}.pptx"

    return Response(
        content=body,
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{urlquote(fname)}"',
        },
    )


@router.get("/changes/{change_id}/signoff/download")
def download_cert_signoff(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream the NPCI Certification Result certificate (.docx) NPCI shipped
    on the all-PASS `cert_completion_signoff` A2A task.

    Bytes live on `incoming_changes.cert_signoff_docx_bytes` (BLOB),
    populated by handlers.cert_completion_signoff. 404 if no sign-off has
    arrived for this change yet.
    """
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if not change.cert_signoff_docx_bytes:
        raise HTTPException(status_code=404, detail="No certification sign-off for this change")

    fname = change.cert_signoff_filename or f"NPCI_Certification_Result_{change_id}.docx"
    return Response(
        content=change.cert_signoff_docx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{urlquote(fname)}"',
        },
    )


@router.get("/changes/{change_id}/documents/{doc_id}/download/xlsx")
def download_change_document_xlsx(
    change_id: str,
    doc_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream the cert_test_cases `.xlsx` workbook NPCI shipped over A2A.

    Bytes live in `change_documents.xlsx_bytes` (BLOB), populated by
    `partner_handlers.handle_change_communication` from the inbound
    `xlsx_b64` field. The partner serves the bytes verbatim so the
    workbook is byte-identical to what NPCI's own download endpoint
    serves (Index / Summary / Modes / archetype sheets, validation
    patches, etc.).
    """
    doc = db.get(ChangeDocument, doc_id)
    if not doc or doc.change_id != change_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.xlsx_bytes:
        raise HTTPException(status_code=404, detail="No .xlsx for this document")

    return Response(
        content=doc.xlsx_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{urlquote(doc.xlsx_filename or f"{doc.doc_type}_v{doc.version}.xlsx")}"'
            ),
        },
    )


@router.get("/changes/{change_id}/documents/{doc_id}/download/zip")
def download_change_document_zip(
    change_id: str,
    doc_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream the multi-schema XSD `.zip` NPCI shipped over A2A.

    Bytes live in `change_documents.zip_bytes` (BLOB), populated from the
    inbound `xsd_zip_b64` when a change touches ≥2 `.xsd` files. The native
    `.xsd` download only returns the first fenced block, so this is the only
    way the partner gets every schema. 404 if the doc has no zip.
    """
    doc = db.get(ChangeDocument, doc_id)
    if not doc or doc.change_id != change_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.zip_bytes:
        raise HTTPException(status_code=404, detail="No .zip for this document")

    fname = doc.zip_filename or f"{doc.doc_type}_v{doc.version}.zip"
    return Response(
        content=doc.zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{urlquote(fname)}"'},
    )


# Artifacts whose single canonical download format is raw text carried in
# `content` (NPCI ships it as a markdown-fenced block). doc_type -> (extension,
# media_type, fence languages to look for).
_NATIVE_FORMATS = {
    "manifest":          ("yaml", "application/x-yaml", ("yaml", "yml")),
    "xsd":               ("xsd",  "application/xml",     ("xsd", "xml")),
    "prototype_screens": ("html", "text/html",          ("html",)),
}


def _extract_fenced(content: str, langs: tuple[str, ...]) -> str:
    """Pull the raw artifact out of NPCI's markdown-fenced content. The kit
    ships e.g. the manifest as a ```yaml block (sometimes preceded by a prose
    summary, as the xsd doc is) — we want just the fenced block, not the fence
    markers or the surrounding markdown. Falls back to the whole content when
    no fence is present."""
    if not content:
        return ""
    # Prefer a fence tagged with the expected language; fall back to any fence.
    for lang in (*langs, r"[A-Za-z0-9]*"):
        m = re.search(rf"```{lang}[^\n]*\n(.*?)```", content, re.DOTALL)
        if m:
            return m.group(1).strip("\n") + "\n"
    return content


@router.get("/changes/{change_id}/documents/{doc_id}/download/native")
def download_change_document_native(
    change_id: str,
    doc_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream a kit artifact in its native single format: manifest -> .yaml,
    xsd -> .xsd, prototype_screens -> .html. The raw artifact is carried as a
    markdown-fenced block inside `content`; extract and serve just that block."""
    doc = db.get(ChangeDocument, doc_id)
    if not doc or doc.change_id != change_id:
        raise HTTPException(status_code=404, detail="Document not found")
    spec = _NATIVE_FORMATS.get(doc.doc_type)
    if not spec:
        raise HTTPException(status_code=404, detail=f"No native format for '{doc.doc_type}'")
    ext, media_type, langs = spec
    if not doc.content:
        raise HTTPException(status_code=404, detail="Document has no content")

    body = _extract_fenced(doc.content, langs).encode("utf-8")
    fname = f"{doc.doc_type}_v{doc.version}.{ext}"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{urlquote(fname)}"',
        },
    )
