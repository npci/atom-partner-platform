# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: settings — NPCI URLs + NPCI-issued API key / JWT / HMAC secrets."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import require_admin
from app.config import settings
from app.core.key_strength import assess_hmac_secret
from app.core.secret_box import SECRET_KEYS, decrypt, encrypt, safe_key_label
from app.database import get_db
from app.models import PartnerSetting, PartnerUser
from app.npci_client import run_npci_reachability_check

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class SettingsUpdateRequest(BaseModel):
    npci_platform_url: str = ""
    # Direct service URL the A2A SDK uses for outbound partner -> NPCI
    # calls (card discovery + JSON-RPC). Distinct from `npci_platform_url`
    # which is the UI-facing URL used only by the connectivity probe. Defaults
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
    # CERT-4: the bank's certification configuration (JSON object), merged one
    # level deep over the demo profile by `handlers/cert_lifecycle.py` when the
    # authority sends cert_config_request. Empty = leave existing unchanged.
    cert_config: str = ""
    # ITA I-6 (§3.5, the instances half of the policy/instances split): the
    # user-supplied certification-trigger URL for the system under test, plus
    # its bearer secret. Validated AT ENTRY — rejected in the UI, not at call
    # time. Empty = leave existing unchanged.
    cert_trigger_url: str = ""
    cert_trigger_secret: str = ""


@router.get("/settings")
def get_settings(user: PartnerUser = Depends(require_admin), db: Session = Depends(get_db)):
    def _get(key, default=""):
        row = db.get(PartnerSetting, key)
        if not row or not row.value:
            return default
        if key in SECRET_KEYS:
            try:
                return decrypt(row.value)
            except Exception:  # noqa: BLE001 — corrupted/tamper-evident; surface as absent
                # Logs a fixed label from secret_box, never the key or the value —
                # see safe_key_label() for why (Checkmarx "Filtering Sensitive Logs").
                logger.critical(
                    "settings: failed to decrypt %s — treating as unconfigured",
                    safe_key_label(key),
                )
                return default
        return row.value

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
        # CERT-4 — not a secret: the operator needs to see what the bank will
        # submit on the next cert_config_request.
        "cert_config": _get("cert_config", ""),
        # ITA I-6: the trigger instance. URL visible; secret masked-only.
        "cert_trigger_url": _get("cert_trigger_url", ""),
        "cert_trigger_secret_masked": _mask(_get("cert_trigger_secret")),
        "has_cert_trigger_secret": bool(_get("cert_trigger_secret")),
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
        # ── HMAC key strength at the INGRESS (CVE-2025-45768) ───────────────
        # `npci_jwt_secret` is the HS256 key that `PartnerAuthMiddleware`
        # verifies every inbound A2A call against, so a weak value here is
        # directly forgeable by anyone who guesses it — a full authentication
        # bypass on the partner ingress. It cannot be validated at startup like
        # SESSION_JWT_SECRET, because it lives in `partner_settings` and is
        # installed at runtime through this endpoint.
        #
        # Validating on WRITE rather than on read is the deliberate choice:
        # rejecting a weak secret at the moment an operator pastes it gives
        # immediate, correctable feedback, whereas a read-time check would
        # fail-closed on every inbound call with the operator unaware of why.
        # NPCI issues these values, so a rejection here is a signal to rotate on
        # the NPCI side — never to weaken this rule.
        _weak = assess_hmac_secret(body.npci_jwt_secret, label="NPCI JWT secret")
        if _weak:
            # 400, not 422: the value is syntactically valid but violates
            # policy. The reasons describe the SHAPE of the secret and never
            # echo it — this response goes back over HTTP, which is one of the
            # ways a credential ends up in a proxy log.
            raise HTTPException(
                status_code=400,
                detail=(
                    "The NPCI JWT secret does not meet HMAC key-strength policy "
                    "(CVE-2025-45768 hardening): "
                    + "; ".join(_weak)
                    + ". This secret is issued by NPCI — rotate it on NPCI admin "
                    "to obtain a compliant value, then paste that here."
                ),
            )
        _upsert(db, "npci_jwt_secret", body.npci_jwt_secret)
        persisted.append("npci_jwt_secret")
        logger.info("Settings update: NPCI JWT secret saved (len=%d)", len(body.npci_jwt_secret))

    if body.npci_hmac_secret:
        # Same policy as npci_jwt_secret above. This key drives the HMAC request
        # signing in `hmac_middleware`/`hmac_signer` — also HMAC-SHA256, also
        # only as strong as the secret behind it. Applying the rule to one of
        # the two HS256 secrets and not the other would leave an equivalent hole
        # open while letting the finding be marked closed.
        _weak = assess_hmac_secret(body.npci_hmac_secret, label="NPCI HMAC secret")
        if _weak:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The NPCI HMAC secret does not meet HMAC key-strength policy "
                    "(CVE-2025-45768 hardening): "
                    + "; ".join(_weak)
                    + ". This secret is issued by NPCI — rotate it on NPCI admin "
                    "to obtain a compliant value, then paste that here."
                ),
            )
        _upsert(db, "npci_hmac_secret", body.npci_hmac_secret)
        persisted.append("npci_hmac_secret")
        logger.info("Settings update: NPCI HMAC secret saved (len=%d)", len(body.npci_hmac_secret))

    # CERT-4: validated on WRITE. The cert_config_request handler falls back
    # to the demo profile on malformed JSON (an inbound A2A reply is no place
    # to error), which means a bad value stored HERE would silently certify
    # the bank against demo values — so this is where it fails loudly.
    if body.cert_config:
        import json as _json

        try:
            parsed = _json.loads(body.cert_config)
            if not isinstance(parsed, dict):
                raise ValueError("cert_config must be a JSON object")
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"cert_config is not valid JSON: {exc}. Nothing was saved.",
            ) from None
        _upsert(db, "cert_config", body.cert_config)
        persisted.append("cert_config")

    # ITA I-6: the trigger INSTANCE is user-supplied but validated at entry
    # (the policy/instances split, §3.5) — a bad URL is rejected in the UI,
    # never discovered mid-suite when the trigger call fails.
    if body.cert_trigger_url:
        from urllib.parse import urlparse

        parsed = urlparse(body.cert_trigger_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(
                status_code=400,
                detail="cert_trigger_url must be an absolute http(s) URL. "
                       "Nothing was saved.",
            )
        from app.npci_client import _is_private_url, _validate_url_scheme

        _validate_url_scheme(body.cert_trigger_url, purpose="certification trigger")

        # SSRF (SAST F-002): scheme/netloc presence is not a destination check.
        # Every inbound `cert_execution_start` auto-dispatches to this address
        # carrying the cert_trigger_secret bearer token — with no human in the
        # loop — so a stored `http://169.254.169.254/…` becomes a credentialed
        # request to the cloud metadata service on NPCI's schedule. Reject at
        # entry, which is where the operator can still act on the message.
        # unresolved=False so configuring a rig that is not deployed yet stays
        # possible — this validates WHERE the URL points, not whether it is up.
        if _is_private_url(body.cert_trigger_url.strip(), unresolved=False):
            raise HTTPException(
                status_code=400,
                detail=(
                    "cert_trigger_url resolves into blocked (loopback/link-local/"
                    "private) address space and was rejected. Every certification "
                    "execution posts the trigger secret to this URL automatically. "
                    "If the rig really is on an internal host, approve it by adding "
                    "the host to NPCI_SSRF_ALLOWED_HOSTS or by setting "
                    "NPCI_SSRF_ALLOW_PRIVATE_NETWORKS=true. Nothing was saved."
                ),
            )
        _upsert(db, "cert_trigger_url", body.cert_trigger_url)
        persisted.append("cert_trigger_url")

    if body.cert_trigger_secret:
        _upsert(db, "cert_trigger_secret", body.cert_trigger_secret)
        persisted.append("cert_trigger_secret")

    db.commit()
    # Log the count only — the list holds secret *field names*, which the SAST
    # taint tracker treats as sensitive even though no value is ever logged.
    logger.info("Settings saved successfully: %d field(s) persisted", len(persisted))
    return {"updated": True, "persisted": persisted}


class TestConnectionRequest(BaseModel):
    """Optional in-flight overrides for the Test Connection button so
    operators can validate a typed-but-not-yet-saved API key.

    Both fields default to None — when omitted, the connectivity probe reads
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
def check_npci_connectivity(
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
    return run_npci_reachability_check(db, api_key_override=api_key_override)


def _upsert(db: Session, key: str, value: str):
    value = value.strip()
    if key in SECRET_KEYS and value:
        # Encrypt at rest (docs/adr/ADR-0002-secrets-vault-migration.md) —
        # Fernet via core/secret_box.py, the same scheme and `enc:v1:` format
        # the NPCI backend uses. Raises SecretBoxError (surfaced as a 500 by
        # the caller's normal exception handling) if PARTNER_SECRET_KEK is
        # unset — fail loudly rather than silently persist plaintext.
        #
        # `encrypt()` is a no-op on an already-encrypted value, so a re-save
        # cannot double-wrap it into an unrecoverable state.
        value = encrypt(value)
    row = db.get(PartnerSetting, key)
    if row:
        row.value = value
    else:
        db.add(PartnerSetting(key=key, value=value))


def _mask(value: str) -> str:
    if not value or len(value) <= 12:
        return "****" if value else ""
    return value[:8] + "****" + value[-4:]
