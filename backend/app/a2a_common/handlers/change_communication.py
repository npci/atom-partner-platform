# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `change_communication`.

Stores the incoming change + Product Kit documents (with checksum verification
of any inline binary attachments) and schedules the post-receipt background
steps (auto-ack, auto cert-status, feasibility). Transport + persistence only —
the feasibility decision runs through the agent registry in `_background`.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import uuid

from sqlalchemy.orm import Session

from app.models import ChangeDocument, IncomingChange

from ._background import schedule_post_receive
from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def _flatten_doc(doc: dict) -> dict:
    """v1.1 wire carries binaries in a uniform ``attachments[]`` array. Flatten
    it back to the legacy ``<kind>_b64`` / ``<kind>_filename`` keys the storage
    loop below already understands, so that loop stays unchanged. A legacy (flat)
    doc is returned as-is."""
    if "attachments" not in doc:
        return doc
    flat = {k: v for k, v in doc.items() if k != "attachments"}
    for att in doc.get("attachments") or []:
        kind = att.get("kind")
        if not kind:
            continue
        if not att.get("omitted"):
            flat[f"{kind}_b64"] = att.get("bytes")
        flat[f"{kind}_filename"] = att.get("filename")
        flat[f"{kind}_sha256"] = att.get("sha256")
        flat[f"{kind}_size_bytes"] = att.get("size_bytes")
        flat[f"{kind}_mime_type"] = att.get("mime_type")
    return flat


def handle_change_communication(body: TaskReceiveRequest, db: Session) -> dict:
    """Process incoming change communication — store change + Product Kit
    documents. Idempotent on `payload.change_id`."""
    payload = body.payload or {}
    npci_change_id = payload.get("change_id", body.change_id or "")
    # v1.1: canonical `kit_version`; fall back to legacy `negotiation_version`.
    incoming_nv = int(payload.get("kit_version") or payload.get("negotiation_version", 1) or 1)

    existing = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == npci_change_id)
        .first()
    )
    if existing:
        # Same or older version → a true duplicate retransmit; skip.
        if incoming_nv <= (existing.negotiation_version or 1):
            logger.info(
                "Change already received: npci_change_id=%s local_id=%s v=%d — skipping",
                npci_change_id, existing.id, incoming_nv,
            )
            return {
                "status": "accepted",
                "message": "Change already received (duplicate skipped)",
                "local_id": existing.id,
            }
        # Higher version → a revision. Bump the mirror, reset acceptance so the
        # "New version available" banner fires, and append the revised docs
        # below (v1 rows are preserved for the version switcher).
        logger.info(
            "Change revision received: npci_change_id=%s local_id=%s v%s→v%d",
            npci_change_id, existing.id, existing.negotiation_version, incoming_nv,
        )
        existing.negotiation_version = incoming_nv
        existing.negotiation_version_accepted = False
        # A new kit version supersedes any prior decision — the partner must
        # re-evaluate v(N) from scratch (accept / query / counter the new terms).
        # Reset decision synchronously here; relying on the best-effort auto-ack
        # to do it is fragile (if PROPOSAL_ACKNOWLEDGED delivery fails a stale
        # decision='accepted' would block counter_propose with "Already
        # accepted"). The auto-ack advances this to 'acknowledged' on success.
        existing.decision = "pending"
        # New kit shipped → the revision NPCI was preparing is done; lift the
        # query hold so the partner can ask about the revised kit.
        existing.revision_in_progress = False
        existing.revision_target_version = None
        change = existing
        change_id = existing.id
    else:
        change_id = str(uuid.uuid4())
        change = IncomingChange(
            id=change_id,
            npci_change_id=npci_change_id,
            title=payload.get("title", "Untitled Change"),
            initial_prompt=payload.get("initial_prompt"),
            enhanced_prompt=payload.get("enhanced_prompt"),
            status="received",
            negotiation_version=incoming_nv,
            negotiation_version_accepted=(incoming_nv == 1),
        )
        db.add(change)

    # Capture NPCI's per-(change, bank) thread correlation_id so every reply we
    # send about this change echoes it back (v1.1 §5). Set on both create and
    # revision — a revision carries the same thread id, but tolerate a late-set.
    if body.correlation_id:
        change.correlation_id = body.correlation_id

    # NPCI's "summary of changes" for this kit version (empty on v1). Stored so
    # the partner change page can show "what changed in this version".
    _summary = payload.get("change_summary")
    if _summary:
        change.npci_change_summary = _summary

    # Build kit_files_received[] for the auto-ack while we iterate. Each
    # entry records whether the bytes we received hash to the value the
    # sender claimed — the rollout-doc non-repudiation receipt.
    kit_files_received: list[dict] = []

    # v1.1: canonical product_kit[] preferred; fall back to legacy documents[].
    _docs = payload.get("product_kit") or payload.get("documents") or []
    for raw_doc in _docs:
        doc = _flatten_doc(raw_doc)
        doc_type = doc.get("doc_type", "unknown")
        content = doc.get("content")

        claimed_content_sha = doc.get("content_sha256")
        if claimed_content_sha and content is not None:
            actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
            kit_files_received.append({
                "doc_type":          doc_type,
                "kind":              "content",
                "checksum_verified": actual == claimed_content_sha,
            })

        # Decode the optional `.docx` attachment if NPCI shipped one.
        # Failures here are non-fatal — the markdown content is still
        # stored, and the download button just won't appear for this
        # doc on the partner UI.
        docx_bytes = None
        docx_filename = doc.get("docx_filename")
        b64 = doc.get("docx_b64")
        if b64:
            try:
                docx_bytes = base64.b64decode(b64)
                claimed_docx_sha = doc.get("docx_sha256")
                if claimed_docx_sha:
                    actual = hashlib.sha256(docx_bytes).hexdigest()
                    kit_files_received.append({
                        "doc_type":          doc_type,
                        "kind":              "docx",
                        "name":              docx_filename,
                        "checksum_verified": actual == claimed_docx_sha,
                    })
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not decode docx attachment doc_type=%s", doc_type, exc_info=True)
                docx_filename = None

        # D7 — companion .pptx (product_deck). Same defensive shape as
        # the docx decode: failure drops the binary but keeps the row.
        pptx_bytes = None
        pptx_filename = doc.get("pptx_filename")
        pb64 = doc.get("pptx_b64")
        if pb64:
            try:
                pptx_bytes = base64.b64decode(pb64)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not decode pptx attachment doc_type=%s", doc.get("doc_type"), exc_info=True)
                pptx_filename = None

        # cert_test_cases — companion .xlsx rendered by the NPCI
        # excel_testcase_engine. Verify SHA-256 over the decoded bytes
        # (matches the docx integrity-check pattern) so a corrupted
        # transfer is recorded in kit_files_received and the partner
        # download serves the exact NPCI workbook.
        xlsx_bytes = None
        xlsx_filename = doc.get("xlsx_filename")
        xb64 = doc.get("xlsx_b64")
        if xb64:
            try:
                xlsx_bytes = base64.b64decode(xb64)
                claimed_xlsx_sha = doc.get("xlsx_sha256")
                if claimed_xlsx_sha:
                    actual = hashlib.sha256(xlsx_bytes).hexdigest()
                    kit_files_received.append({
                        "doc_type":          doc_type,
                        "kind":              "xlsx",
                        "name":              xlsx_filename,
                        "checksum_verified": actual == claimed_xlsx_sha,
                    })
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not decode xlsx attachment doc_type=%s", doc.get("doc_type"), exc_info=True)
                xlsx_filename = None

        # Promo/explainer video (.mp4). Same defensive shape; reject oversize
        # (>25 MB decoded) so a malformed/huge attachment can't bloat the row.
        video_bytes = None
        video_filename = doc.get("video_filename")
        vb64 = doc.get("video_b64")
        if vb64:
            try:
                decoded = base64.b64decode(vb64)
                if len(decoded) > 25 * 1024 * 1024:
                    logger.warning("Dropping oversize video attachment doc_type=%s (%d bytes)",
                                   doc_type, len(decoded))
                    video_filename = None
                else:
                    video_bytes = decoded
                    claimed_video_sha = doc.get("video_sha256")
                    if claimed_video_sha:
                        actual = hashlib.sha256(video_bytes).hexdigest()
                        kit_files_received.append({
                            "doc_type":          doc_type,
                            "kind":              "video",
                            "name":              video_filename,
                            "checksum_verified": actual == claimed_video_sha,
                        })
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not decode video attachment doc_type=%s", doc_type, exc_info=True)
                video_filename = None

        # XSD multi-schema bundle (.zip) — shipped when the change touches ≥2
        # .xsd files. Same defensive decode + checksum shape as the others.
        zip_bytes = None
        zip_filename = doc.get("xsd_zip_filename")
        zb64 = doc.get("xsd_zip_b64")
        if zb64:
            try:
                decoded = base64.b64decode(zb64)
                if len(decoded) > 10 * 1024 * 1024:
                    logger.warning("Dropping oversize xsd zip attachment doc_type=%s (%d bytes)",
                                   doc_type, len(decoded))
                    zip_filename = None
                    decoded = None
                zip_bytes = decoded
                claimed_zip_sha = doc.get("xsd_zip_sha256") if zip_bytes else None
                if claimed_zip_sha:
                    actual = hashlib.sha256(zip_bytes).hexdigest()
                    kit_files_received.append({
                        "doc_type":          doc_type,
                        "kind":              "zip",
                        "name":              zip_filename,
                        "checksum_verified": actual == claimed_zip_sha,
                    })
            except Exception as exc:  # noqa: BLE001
                # Type only — this decodes PARTNER-SUPPLIED bytes, so the
                # exception message can carry attacker-influenced content
                # (crafted filenames from the zip central directory) straight
                # into the log (CWE-209 / log injection). Detail at DEBUG.
                logger.warning(
                    "Could not decode xsd zip attachment doc_type=%s: %s",
                    doc_type, type(exc).__name__,
                )
                logger.debug("xsd zip decode failure detail doc_type=%s", doc_type, exc_info=True)
                zip_filename = None

        db.add(ChangeDocument(
            change_id=change_id,
            doc_type=doc_type,
            content=content,
            version=doc.get("version", 1),
            negotiation_version=incoming_nv,
            docx_filename=docx_filename,
            docx_bytes=docx_bytes,
            pptx_filename=pptx_filename,
            pptx_bytes=pptx_bytes,
            xlsx_filename=xlsx_filename,
            xlsx_bytes=xlsx_bytes,
            video_filename=video_filename,
            video_bytes=video_bytes,
            zip_filename=zip_filename,
            zip_bytes=zip_bytes,
        ))

    db.commit()
    doc_count = len(_docs)
    logger.info(
        "Change received: id=%s title='%s' docs=%d",
        change_id, change.title, doc_count,
    )

    # Schedule the post-receipt background steps (WS11): auto-ack +
    # CERT_STATUS_UPDATE(received) + feasibility analysis. Scheduled on the
    # event loop via asyncio.to_thread (see `_background`), not raw daemon
    # threads — so blocking work stays off the loop and failures are visible
    # (feasibility failures land as `agent_runs` rows). The inbound SDK
    # request returns promptly; all three steps are best-effort.
    kit_id = payload.get("kit_id")
    schedule_post_receive(
        npci_change_id, change_id, kit_id, incoming_nv,
        body.message_id, kit_files_received,
    )

    return {
        "status": "accepted",
        "message": f"Change received with {doc_count} documents",
        "local_id": change_id,
    }
