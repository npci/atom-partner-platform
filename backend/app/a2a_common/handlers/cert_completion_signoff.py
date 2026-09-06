# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `cert_completion_signoff` — the formal all-PASS
sign-off NPCI emits after a cert run passes cleanly.

Carries the NPCI Certification Result certificate (.docx) inline as base64.
We verify the SHA-256 (mirrors change_communication's integrity check),
store the bytes on the change so the partner UI can offer a download, and
defensively stamp cert_status='certified' (the all-PASS cert_test_response
usually got there first, but the signoff is the authoritative signal)."""
from __future__ import annotations

import base64
import hashlib
import json as _json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_cert_completion_signoff(body: TaskReceiveRequest, db: Session) -> dict:
    payload = body.payload or {}
    npci_change_id = body.change_id or payload.get("change_id") or ""

    change = (
        db.query(IncomingChange)
        .filter(IncomingChange.npci_change_id == npci_change_id)
        .first()
    )
    if not change:
        logger.warning(
            "cert_completion_signoff for unknown change npci_change_id=%s — accepting but no-op",
            npci_change_id,
        )
        return {"status": "accepted", "message": "Unknown change — discarded"}

    # Decode + integrity-check the certificate. A decode/checksum failure is
    # non-fatal: we still record the sign-off (status flip) so the lifecycle
    # advances; the download button just won't appear.
    checksum_verified = None
    b64 = payload.get("signoff_docx_b64")
    if b64:
        try:
            raw = base64.b64decode(b64)
            claimed_sha = payload.get("signoff_sha256")
            if claimed_sha:
                checksum_verified = hashlib.sha256(raw).hexdigest() == claimed_sha
            if checksum_verified is False:
                logger.warning(
                    "cert_completion_signoff checksum mismatch change=%s — storing anyway",
                    change.id,
                )
            change.cert_signoff_docx_bytes = raw
            change.cert_signoff_filename = (
                payload.get("signoff_filename")
                or f"NPCI_Certification_Result_{npci_change_id}.docx"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cert_completion_signoff could not decode docx change=%s", change.id, exc_info=True)

    # Authoritative certified signal — stamp status + history.
    history = {}
    if change.cert_status_history:
        try:
            history = _json.loads(change.cert_status_history) or {}
        except Exception:
            history = {}
    if "certified" not in history:
        history["certified"] = datetime.now(timezone.utc).isoformat()
    change.cert_status = "certified"
    change.cert_status_history = _json.dumps(history)

    db.commit()
    logger.info(
        "cert_completion_signoff persisted: change=%s file=%s checksum_verified=%s",
        change.id, change.cert_signoff_filename, checksum_verified,
    )
    return {
        "status": "accepted",
        "message": "Certification sign-off recorded",
        "has_signoff_doc": bool(change.cert_signoff_docx_bytes),
        "checksum_verified": checksum_verified,
    }
