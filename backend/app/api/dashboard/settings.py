"""Dashboard domain: settings — NPCI URLs + NPCI-issued API key / JWT / HMAC secrets."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import require_admin
from app.config import settings
from app.database import get_db
from app.models import PartnerSetting, PartnerUser
from app.npci_client import test_connection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class SettingsUpdateRequest(BaseModel):
    npci_platform_url: str = ""
    # Direct service URL the A2A SDK uses for outbound partner -> NPCI
    # calls (card discovery + JSON-RPC). Distinct from `npci_platform_url`
    # which is the UI-facing URL used only by `test_connection`. Defaults
    # to the docker service name `http://npci_backend:8000` when unset,
    # which works in docker-compose but fails on host-mode deployments
    # (Ubuntu native) — operator must override there.
    npci_a2a_url: str = ""
    partner_api_key: str = ""
    partner_name: str = ""
    partner_anthropic_api_key: str = ""
    # Slice 3 / 5 of A2A security hardening — the two long-lived
    # NPCI-issued secrets the partner installs so its inbound A2A
    # middlewares can validate calls. Empty string = leave existing
    # value unchanged. "****" prefix = masked echo, also ignored.
    npci_jwt_secret: str = ""
    npci_hmac_secret: str = ""


@router.get("/settings")
def get_settings(user: PartnerUser = Depends(require_admin), db: Session = Depends(get_db)):
    def _get(key, default=""):
        row = db.get(PartnerSetting, key)
        return row.value if row else default

    anthropic_key = _get("partner_anthropic_api_key") or settings.partner_anthropic_api_key
    npci_jwt_secret  = _get("npci_jwt_secret")
    npci_hmac_secret = _get("npci_hmac_secret")
    return {
        "npci_platform_url": _get("npci_platform_url", "http://localhost"),
        "npci_a2a_url":      _get("npci_a2a_url", ""),
        "partner_api_key_masked": _mask(_get("partner_api_key")),
        "partner_name": _get("partner_name", "Partner Agent"),
        "has_api_key": bool(_get("partner_api_key")),
        "partner_anthropic_api_key_masked": _mask(anthropic_key),
        "has_anthropic_api_key": bool(anthropic_key),
        # Slice 3 / 5 — secrets are NEVER returned plaintext, only
        # their masked form + a presence flag. The frontend uses the
        # flag to render "configured / not configured" badges.
        "npci_jwt_secret_masked": _mask(npci_jwt_secret),
        "has_npci_jwt_secret": bool(npci_jwt_secret),
        "npci_hmac_secret_masked": _mask(npci_hmac_secret),
        "has_npci_hmac_secret": bool(npci_hmac_secret),
    }


@router.put("/settings")
def update_settings(body: SettingsUpdateRequest, user: PartnerUser = Depends(require_admin), db: Session = Depends(get_db)):
    logger.info(
        "Settings update: url='%s' name='%s' key_len=%d anthropic_len=%d",
        body.npci_platform_url, body.partner_name, len(body.partner_api_key), len(body.partner_anthropic_api_key),
    )

    # Reject masked-echo pastes BEFORE saving anything. Previously these
    # were silently skipped per-field — leading to a misleading "Settings
    # saved" toast even when the operator had pasted NPCI's masked
    # display ('a2a_xxxx****xxxx') into the api_key field and the real
    # value never landed in the DB. Fail loudly + atomically so the
    # operator sees the problem before assuming the save worked.
    secret_fields = {
        "partner_api_key":             body.partner_api_key,
        "partner_anthropic_api_key":   body.partner_anthropic_api_key,
        "npci_jwt_secret":             body.npci_jwt_secret,
        "npci_hmac_secret":            body.npci_hmac_secret,
    }
    masked_fields = [name for name, val in secret_fields.items() if val and "****" in val]
    if masked_fields:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Masked value detected in {masked_fields}. You pasted the masked display "
                f"(e.g. 'a2a_xxxx****xxxx') instead of the real secret. NPCI shows real "
                f"secret values only ONCE — at create time or after Rotate. Rotate the "
                f"affected credential on NPCI admin to obtain the full value, then paste THAT."
            ),
        )

    persisted: list[str] = []

    # Save all non-empty fields
    if body.npci_platform_url:
        _upsert(db, "npci_platform_url", body.npci_platform_url)
        persisted.append("npci_platform_url")
    if body.npci_a2a_url:
        _upsert(db, "npci_a2a_url", body.npci_a2a_url)
        persisted.append("npci_a2a_url")
    if body.partner_name:
        _upsert(db, "partner_name", body.partner_name)
        persisted.append("partner_name")

    # Non-empty secret values — masked pastes were already rejected above.
    if body.partner_api_key:
        _upsert(db, "partner_api_key", body.partner_api_key)
        persisted.append("partner_api_key")
        logger.info("Settings update: NPCI API key saved (len=%d)", len(body.partner_api_key))

    if body.partner_anthropic_api_key:
        _upsert(db, "partner_anthropic_api_key", body.partner_anthropic_api_key)
        persisted.append("partner_anthropic_api_key")
        logger.info("Settings update: Anthropic API key saved (len=%d)", len(body.partner_anthropic_api_key))

    # Slice 3 / 5 — install the per-partner JWT and HMAC secrets that
    # NPCI issued at partner-create or rotate time. We log only the
    # length, never the value.
    if body.npci_jwt_secret:
        _upsert(db, "npci_jwt_secret", body.npci_jwt_secret)
        persisted.append("npci_jwt_secret")
        logger.info("Settings update: NPCI JWT secret saved (len=%d)", len(body.npci_jwt_secret))

    if body.npci_hmac_secret:
        _upsert(db, "npci_hmac_secret", body.npci_hmac_secret)
        persisted.append("npci_hmac_secret")
        logger.info("Settings update: NPCI HMAC secret saved (len=%d)", len(body.npci_hmac_secret))

    db.commit()
    # Log the count only — the list holds secret *field names*, which the SAST
    # taint tracker treats as sensitive even though no value is ever logged.
    logger.info("Settings saved successfully: %d field(s) persisted", len(persisted))
    return {"updated": True, "persisted": persisted}


class TestConnectionRequest(BaseModel):
    """Optional in-flight overrides for the Test Connection button so
    operators can validate a typed-but-not-yet-saved API key.

    Both fields default to None — when omitted, `test_connection()` reads
    from the stored partner_settings rows (current "configured state"
    check). When provided, the override is used for THIS test only;
    nothing is persisted until the operator clicks Save.

    Masked echoes from the frontend (anything containing "****") are
    treated as "not provided" so a re-test from an already-configured
    state doesn't accidentally treat the mask as the secret.
    """
    url: str | None = None
    api_key: str | None = None


@router.post("/settings/test-connection")
def test_npci_connection(
    body: TestConnectionRequest | None = None,
    user: PartnerUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # If the typed value contains "****" it's the masked display the
    # operator copied off the NPCI admin Partners list (mask format is
    # <8>****<4>). Real api_keys are token_urlsafe — they can't contain
    # asterisks — so this check has no false positives. Fail fast with
    # a specific message instead of silently falling through to the DB
    # (which yields the misleading "Partner API Key is not configured"
    # toast even though the operator clearly pasted something).
    if body and body.api_key and "****" in body.api_key:
        return {
            "status": "error",
            "message": "You pasted the masked display (e.g. 'a2a_xxxx****xxxx'), "
                       "not the real API key. NPCI shows the real value only ONCE "
                       "at partner-create or after Rotate Key. Click 'Rotate Key' "
                       "on the NPCI admin Partners page to get the full value, then "
                       "paste THAT here.",
        }

    api_key_override = body.api_key if (body and body.api_key) else None
    return test_connection(db, api_key_override=api_key_override)


def _upsert(db: Session, key: str, value: str):
    value = value.strip()
    row = db.get(PartnerSetting, key)
    if row:
        row.value = value
    else:
        db.add(PartnerSetting(key=key, value=value))


def _mask(value: str) -> str:
    if not value or len(value) <= 12:
        return "****" if value else ""
    return value[:8] + "****" + value[-4:]
