# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: partner profile (PARTNER.md) — UI-configurable.

The partner's capability profile used to be a read-only file on disk. It is now
DB-backed (the active `partner_profiles` row) and editable from the Settings UI:
operators upload a `.md` or edit it in-app, and the feasibility analyser +
design/code/testing agents read it via `agents/_common.read_partner_profile()`.

One partner per deployment → a single active row, upserted on save.

Endpoints:
  GET /api/profile  — the active profile (DB row, else the seed/mounted file)
  PUT /api/profile  — save the edited / uploaded markdown
"""
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents._common import parse_frontmatter
from app.api.auth import require_admin
from app.config import settings
from app.database import get_db
from app.models import PartnerProfile, PartnerUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _active_profile(db: Session) -> PartnerProfile | None:
    """The single active profile row (one partner per deployment)."""
    return db.query(PartnerProfile).first()


def _file_fallback() -> str:
    """The mounted seed file's content, or '' — used to pre-populate the editor
    on a fresh deploy before the first save lands a DB row."""
    from pathlib import Path
    path = Path(settings.partner_profile_path)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


@router.get("/profile")
def get_profile(user: PartnerUser = Depends(require_admin), db: Session = Depends(get_db)):
    """Return the active partner profile for the editor. Falls back to the
    mounted file's content (source='file') when no DB row exists yet."""
    row = _active_profile(db)
    if row is not None:
        return {
            "exists": True,
            "partner_name": row.partner_name,
            "profile_version": row.profile_version,
            "content": row.content,
            "source": row.source,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "size_bytes": len(row.content.encode("utf-8")),
        }

    raw = _file_fallback()
    fm = parse_frontmatter(raw)
    return {
        "exists": False,
        "partner_name": fm.get("partner"),
        "profile_version": fm.get("profile_version"),
        "content": raw,
        "source": "file",
        "updated_at": None,
        "size_bytes": len(raw.encode("utf-8")),
    }


class ProfileUpdateRequest(BaseModel):
    content: str
    # Optional — when omitted, derived from the markdown frontmatter (`partner`
    # / `profile_version`). Lets the UI either pass explicit field values or
    # rely on whatever the uploaded `.md` declares.
    partner_name: str | None = None
    profile_version: str | None = None
    # 'edit' (in-app) or 'upload' (a .md was uploaded). Display-only.
    source: str = "edit"


@router.put("/profile")
def update_profile(
    body: ProfileUpdateRequest,
    user: PartnerUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Upsert the single active profile row. Derives partner_name /
    profile_version from frontmatter when the client didn't supply them."""
    fm = parse_frontmatter(body.content)
    partner_name = body.partner_name or fm.get("partner")
    profile_version = body.profile_version or fm.get("profile_version")
    source = body.source if body.source in ("edit", "upload") else "edit"

    row = _active_profile(db)
    if row is None:
        row = PartnerProfile(content=body.content)
        db.add(row)
    row.content = body.content
    row.partner_name = partner_name
    row.profile_version = profile_version
    row.source = source
    db.commit()
    db.refresh(row)

    logger.info(
        "Partner profile saved: partner=%r version=%r source=%s len=%d",
        partner_name, profile_version, source, len(body.content),
    )
    return {
        "saved": True,
        "partner_name": row.partner_name,
        "profile_version": row.profile_version,
        "source": row.source,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "size_bytes": len(row.content.encode("utf-8")),
    }
