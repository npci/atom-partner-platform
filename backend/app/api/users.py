# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""User management endpoints (admin only)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import _validate_new_password, hash_password, require_admin
from app.database import get_db
from app.models import PartnerUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    username: str
    password: str
    full_name: str = ""
    role: str = "user"


class UpdateUserRequest(BaseModel):
    full_name: str | None = None
    role: str | None = None
    password: str | None = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
def list_users(
    admin: PartnerUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.scalars(select(PartnerUser).order_by(PartnerUser.created_at)).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("")
def create_user(
    body: CreateUserRequest,
    admin: PartnerUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.scalars(
        select(PartnerUser).where(PartnerUser.username == body.username)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")

    _validate_new_password(body.password)

    user = PartnerUser(
        username=body.username,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
    )
    db.add(user)
    db.commit()

    logger.info("User created: %s by %s", body.username, admin.username)
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
    }


@router.put("/{user_id}")
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    admin: PartnerUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(PartnerUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.full_name is not None:
        user.full_name = body.full_name
    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
        user.role = body.role
    if body.password is not None:
        _validate_new_password(body.password)
        user.password_hash = hash_password(body.password)

    db.commit()
    logger.info("User updated: %s by %s", user.username, admin.username)
    return {"updated": True}


@router.delete("/{user_id}")
def deactivate_user(
    user_id: str,
    admin: PartnerUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(PartnerUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    user.is_active = False
    db.commit()
    logger.info("User deactivated: %s by %s", user.username, admin.username)
    return {"deactivated": True}
