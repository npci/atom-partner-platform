"""Authentication endpoints and JWT helpers."""
import hashlib
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import PartnerUser

# Standard password policy — ≥8 chars + ≥1 letter + ≥1 digit. Mirrors
# the NPCI backend so partner operators see identical validation
# messages whichever side they sign up on.
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT  = re.compile(r"\d")


def _validate_new_password(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if not _HAS_LETTER.search(pw):
        raise HTTPException(status_code=422, detail="Password must contain at least one letter")
    if not _HAS_DIGIT.search(pw):
        raise HTTPException(status_code=422, detail="Password must contain at least one digit")

logger = logging.getLogger(__name__)

# Local login-session signing key. Sourced from config/env (SESSION_JWT_SECRET),
# NOT hardcoded — a per-deployment secret keeps sessions from being forgeable
# across installs. We warn loudly when it's left at the insecure placeholder so
# operators don't ship the default. (Distinct from the NPCI-issued
# `npci_jwt_secret` used to validate inbound A2A calls.)
JWT_SECRET = settings.session_jwt_secret
# Insecure-default warning is now emitted by config.py (which also blocks
# production startup entirely). No duplicate warning here.
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24  # 24h session. The longer 7-day variant was reverted
                       # pre-merge; mid-session 401s should be solved by
                       # client-side refresh, not a 7× longer token lifetime
                       # (stolen-token blast radius goes from 1 day to 1 week).

# Session is carried in an httpOnly cookie so the JWT is never reachable from
# JavaScript (closes the "sensitive data in web storage" finding — a stolen
# XSS payload can no longer read the token out of localStorage). A Bearer
# header is still accepted as a fallback for non-browser callers.
COOKIE_NAME = "pp_session"

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _extract_token(request: Request) -> str | None:
    """Pull the session JWT from the httpOnly cookie, falling back to a
    Bearer Authorization header (non-browser clients)."""
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        return tok
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() or None
    return None


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=JWT_EXPIRY_HOURS * 3600,
        httponly=True,
        # Secure only in production — the dev stack serves plain HTTP, where a
        # Secure cookie would never be sent back.
        secure=(settings.app_env == "production"),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite="lax")


# ── JWT token denylist (in-memory, single-instance) ─────────────────────────
# Stores SHA-256 hashes of revoked tokens with their expiry timestamps.
# Cleaned up lazily — entries are purged once their TTL has elapsed.
_token_denylist: dict[str, float] = {}  # {sha256_hex: expiry_epoch}
_denylist_lock = threading.Lock()


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def revoke_token(raw_token: str) -> None:
    """Add a token's hash to the denylist until its natural expiry."""
    try:
        payload = jwt.decode(raw_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        exp = payload.get("exp", 0)
    except JWTError:
        # Token already invalid — nothing to revoke
        return
    with _denylist_lock:
        _token_denylist[_token_hash(raw_token)] = float(exp)
        # Lazy cleanup: purge expired entries
        now = time.time()
        expired = [k for k, v in _token_denylist.items() if v < now]
        for k in expired:
            del _token_denylist[k]


def is_token_revoked(raw_token: str) -> bool:
    h = _token_hash(raw_token)
    with _denylist_lock:
        if h in _token_denylist:
            if _token_denylist[h] < time.time():
                del _token_denylist[h]
                return False
            return True
    return False


# ── Login brute-force protection (in-memory) ────────────────────────────────
# Tracks consecutive failures per username and per client IP.
_login_failures: dict[str, tuple[int, float]] = {}  # {key: (count, last_fail_epoch)}
_failures_lock = threading.Lock()

_LOCKOUT_TIER_1 = (5, 60)       # 5 failures → 60s lockout
_LOCKOUT_TIER_2 = (10, 300)     # 10 failures → 5min lockout


def _check_lockout(key: str) -> None:
    """Raise 429 if the key is currently locked out."""
    with _failures_lock:
        entry = _login_failures.get(key)
        if not entry:
            return
        count, last_fail = entry
        now = time.time()
        if count >= _LOCKOUT_TIER_2[0] and (now - last_fail) < _LOCKOUT_TIER_2[1]:
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 5 minutes.")
        if count >= _LOCKOUT_TIER_1[0] and (now - last_fail) < _LOCKOUT_TIER_1[1]:
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 60 seconds.")


def _record_failure(key: str) -> None:
    with _failures_lock:
        entry = _login_failures.get(key)
        count = (entry[0] if entry else 0) + 1
        _login_failures[key] = (count, time.time())


def _clear_failures(*keys: str) -> None:
    with _failures_lock:
        for k in keys:
            _login_failures.pop(k, None)


# ── Helpers ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user: PartnerUser) -> str:
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> PartnerUser:
    """Dependency that validates the session JWT (cookie or Bearer header)
    and returns the current user."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if is_token_revoked(token):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.get(PartnerUser, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_admin(user: PartnerUser = Depends(get_current_user)) -> PartnerUser:
    """Dependency that requires admin role."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Schemas ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    # Token is delivered via an httpOnly cookie, not the body.
    user: dict


class UserInfo(BaseModel):
    id: str
    username: str
    full_name: str | None
    role: str
    is_active: bool


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    user_key = f"user:{body.username}"
    ip_key = f"ip:{client_ip}"

    _check_lockout(user_key)
    _check_lockout(ip_key)

    user = db.scalars(
        select(PartnerUser).where(PartnerUser.username == body.username)
    ).first()

    if not user or not verify_password(body.password, user.password_hash):
        _record_failure(user_key)
        _record_failure(ip_key)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account is deactivated")

    _clear_failures(user_key, ip_key)
    token = create_token(user)
    _set_session_cookie(response, token)
    logger.info("User logged in: %s", user.username)
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
        },
    }


@router.get("/me")
def get_me(user: PartnerUser = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
    }


@router.post("/logout")
def logout(request: Request, response: Response):
    token = _extract_token(request)
    if token:
        revoke_token(token)
    _clear_session_cookie(response)
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the authenticated user's password. JWT remains valid
    afterwards — same identity, no forced re-login."""
    if not verify_password(body.current_password, user.password_hash):
        logger.warning("Change-password failed: user=%s reason=bad_current", user.username)
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    _validate_new_password(body.new_password)

    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(status_code=422, detail="New password must differ from the current one")

    user.password_hash = hash_password(body.new_password)
    db.commit()
    logger.info("Password changed: user=%s", user.username)
    return {"detail": "Password changed"}


# ── Seed default admin ──────────────────────────────────────────────────────

def seed_admin(db: Session):
    """Create default admin user if no users exist."""
    count = db.scalars(select(PartnerUser)).first()
    if count is None:
        password = secrets.token_urlsafe(16)
        admin = PartnerUser(
            username="admin",
            password_hash=hash_password(password),
            full_name="Administrator",
            role="admin",
            is_active=True,
        )
        db.add(admin)
        db.commit()
        logger.info("Default admin user created (username: admin)")
        print(f"\n  Default admin password: {password}\n  ⚠️ SAVE THIS — shown once only\n")
