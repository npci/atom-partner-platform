# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: settings — authority URLs + authority-issued API key / JWT / HMAC secrets."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import require_admin
from app.config import settings
from app.core.key_strength import assess_hmac_secret
from app.core.secret_box import SECRET_KEYS, SecretBoxError, decrypt, encrypt, safe_key_label
from app.database import get_db
from app.models import PartnerSetting, PartnerUser
from app.authority_client import run_authority_reachability_check

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _validate_authority_url(raw: str, field: str) -> None:
    """Reject an authority URL that is malformed or points into blocked space.

    Both authority URLs carry credentials outbound: `authority_platform_url` is
    probed with the partner API key in the body, and `authority_a2a_url` is the
    address every A2A send posts to, carrying the session-minted JWT and the
    HMAC envelope. Until now neither was checked at all on this path — only
    `cert_trigger_url` below was — so a stored `http://169.254.169.254/` was a
    supported configuration and the only complaint was a log line.

    `authority_platform_url` additionally decides the SSRF guard's
    configured-authority waiver (`authority_client._configured_authority_host`),
    so leaving it unvalidated would let whoever can write settings choose which
    private host the guard permits.
    """
    from urllib.parse import urlparse

    from app.authority_client import _is_private_url, _validate_url_scheme

    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be an absolute http(s) URL. Nothing was saved.",
        )
    _validate_url_scheme(url, purpose=field)
    # unresolved=False so an authority that is not deployed yet stays
    # configurable — this validates WHERE the URL points, not whether it is up.
    if _is_private_url(url, unresolved=False):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field} resolves into blocked (loopback/link-local/private) "
                "address space and was rejected. If the authority really is on "
                "an internal host, approve it by adding the host to "
                "AUTHORITY_SSRF_ALLOWED_HOSTS or by setting "
                "AUTHORITY_SSRF_ALLOW_PRIVATE_NETWORKS=true. Nothing was saved."
            ),
        )


class SettingsUpdateRequest(BaseModel):
    authority_platform_url: str = ""
    # Direct service URL the A2A SDK uses for outbound partner -> authority
    # calls (card discovery + JSON-RPC). Distinct from `authority_platform_url`
    # which is the UI-facing URL used only by the connectivity probe. Defaults
    # to the docker service name `http://authority_backend:8000` when unset,
    # which works in docker-compose but fails on host-mode deployments
    # (Ubuntu native) — operator must override there.
    authority_a2a_url: str = ""
    partner_api_key: str = ""
    partner_name: str = ""
    partner_anthropic_api_key: str = ""
    partner_openai_api_key: str = ""
    partner_ainxt_api_key: str = ""
    # A2A security hardening: the two long-lived
    # authority-issued secrets the partner installs so its inbound A2A
    # middlewares can validate calls. Empty string = leave existing
    # value unchanged. "****" prefix = masked echo, also ignored.
    authority_jwt_secret: str = ""
    authority_hmac_secret: str = ""
    # CERT-4: the partner's certification configuration (JSON object), merged one
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
                # see safe_key_label() for why (sensitive-log filtering).
                logger.critical(
                    "settings: failed to decrypt %s — treating as unconfigured",
                    safe_key_label(key),
                )
                return default
        return row.value

    anthropic_key = _get("partner_anthropic_api_key") or settings.partner_anthropic_api_key
    openai_key = _get("partner_openai_api_key") or settings.partner_openai_api_key
    ainxt_key = _get("partner_ainxt_api_key") or settings.partner_ainxt_api_key
    authority_jwt_secret  = _get("authority_jwt_secret")
    authority_hmac_secret = _get("authority_hmac_secret")

    # AI readiness follows the CONFIGURED provider, not Anthropic. `has_anthropic_api_key`
    # below used to be the platform's ONLY AI-readiness signal, so an OpenAI or AiNxt
    # deployment reported "not configured" forever while happily calling its LLM.
    # Imported here rather than at module scope to keep the settings API off the LLM
    # import chain. `get_provider()` canonicalizes, so this reads "anthropic" even when
    # the operator wrote the legacy `LLM_PROVIDER=claude`.
    from app.core.llm import get_provider
    llm_provider = get_provider()
    has_llm_api_key = bool({
        "anthropic": anthropic_key,
        "openai": openai_key,
        "ainxt":  ainxt_key,
    }.get(llm_provider, ""))
    return {
        "authority_platform_url": _get("authority_platform_url", "http://localhost"),
        "authority_a2a_url":      _get("authority_a2a_url", ""),
        "partner_api_key_masked": _mask(_get("partner_api_key")),
        "partner_name": _get("partner_name", "Partner Agent"),
        "has_api_key": bool(_get("partner_api_key")),
        "partner_anthropic_api_key_masked": _mask(anthropic_key),
        "llm_provider": llm_provider,
        "has_llm_api_key": has_llm_api_key,
        # Legacy alias, still emitted for one release so a stale cached frontend
        # bundle keeps rendering. New callers must read `has_llm_api_key`.
        "has_anthropic_api_key": bool(anthropic_key),
        "partner_openai_api_key_masked": _mask(openai_key),
        "has_openai_api_key": bool(openai_key),
        "partner_ainxt_api_key_masked": _mask(ainxt_key),
        "has_ainxt_api_key": bool(ainxt_key),
        # Secrets are NEVER returned plaintext, only
        # their masked form + a presence flag. The frontend uses the
        # flag to render "configured / not configured" badges.
        "authority_jwt_secret_masked": _mask(authority_jwt_secret),
        "has_authority_jwt_secret": bool(authority_jwt_secret),
        "authority_hmac_secret_masked": _mask(authority_hmac_secret),
        "has_authority_hmac_secret": bool(authority_hmac_secret),
        # CERT-4 — not a secret: the operator needs to see what the partner will
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
        "Settings update: url='%s' name='%s' key_len=%d llm_key_lengths=%s",
        body.authority_platform_url, body.partner_name, len(body.partner_api_key),
        {"anthropic": len(body.partner_anthropic_api_key),
         "openai": len(body.partner_openai_api_key), "ainxt": len(body.partner_ainxt_api_key)},
    )

    # Reject masked-echo pastes BEFORE saving anything. Previously these
    # were silently skipped per-field — leading to a misleading "Settings
    # saved" toast even when the operator had pasted the authority's masked
    # display ('a2a_xxxx****xxxx') into the api_key field and the real
    # value never landed in the DB. Fail loudly + atomically so the
    # operator sees the problem before assuming the save worked.
    secret_fields = {
        "partner_api_key":             body.partner_api_key,
        "partner_anthropic_api_key":   body.partner_anthropic_api_key,
        "partner_openai_api_key":      body.partner_openai_api_key,
        "partner_ainxt_api_key":       body.partner_ainxt_api_key,
        "authority_jwt_secret":             body.authority_jwt_secret,
        "authority_hmac_secret":            body.authority_hmac_secret,
    }
    masked_fields = [name for name, val in secret_fields.items() if val and "****" in val]
    if masked_fields:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Masked value detected in {masked_fields}. You pasted the masked display "
                f"(e.g. 'a2a_xxxx****xxxx') instead of the real secret. The authority shows "
                f"real secret values only ONCE — at create time or after Rotate. Rotate the "
                f"affected credential on the authority's admin console to obtain the full "
                f"value, then paste THAT."
            ),
        )

    persisted: list[str] = []

    # Save all non-empty fields
    if body.authority_platform_url:
        _validate_authority_url(body.authority_platform_url, "authority_platform_url")
        _upsert(db, "authority_platform_url", body.authority_platform_url)
        persisted.append("authority_platform_url")
    if body.authority_a2a_url:
        _validate_authority_url(body.authority_a2a_url, "authority_a2a_url")
        _upsert(db, "authority_a2a_url", body.authority_a2a_url)
        persisted.append("authority_a2a_url")
    if body.partner_name:
        _upsert(db, "partner_name", body.partner_name)
        persisted.append("partner_name")

    # Non-empty secret values — masked pastes were already rejected above.
    if body.partner_api_key:
        _upsert(db, "partner_api_key", body.partner_api_key)
        persisted.append("partner_api_key")
        logger.info("Settings update: authority API key saved (len=%d)", len(body.partner_api_key))

    provider_keys = {
        "anthropic": ("partner_anthropic_api_key", body.partner_anthropic_api_key),
        "openai": ("partner_openai_api_key", body.partner_openai_api_key),
        "ainxt": ("partner_ainxt_api_key", body.partner_ainxt_api_key),
    }
    for provider, (setting_key, value) in provider_keys.items():
        if value:
            _upsert(db, setting_key, value)
            persisted.append(setting_key)
            logger.info("Settings update: %s API key saved (len=%d)", provider, len(value))

    # Install the per-partner JWT and HMAC secrets that
    # the authority issued at partner-create or rotate time. We log only the
    # length, never the value.
    if body.authority_jwt_secret:
        # ── HMAC key strength at the INGRESS (CVE-2025-45768) ───────────────
        # `authority_jwt_secret` is the HS256 key that `PartnerAuthMiddleware`
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
        # The authority issues these values, so a rejection here is a signal to
        # rotate on the authority side — never to weaken this rule.
        _weak = assess_hmac_secret(body.authority_jwt_secret, label="authority JWT secret")
        if _weak:
            # 400, not 422: the value is syntactically valid but violates
            # policy. The reasons describe the SHAPE of the secret and never
            # echo it — this response goes back over HTTP, which is one of the
            # ways a credential ends up in a proxy log.
            raise HTTPException(
                status_code=400,
                detail=(
                    "The authority JWT secret does not meet HMAC key-strength "
                    "policy (CVE-2025-45768 hardening): "
                    + "; ".join(_weak)
                    + ". This secret is issued by the authority — rotate it on "
                    "the authority's admin console to obtain a compliant value, "
                    "then paste that here."
                ),
            )
        _upsert(db, "authority_jwt_secret", body.authority_jwt_secret)
        persisted.append("authority_jwt_secret")
        logger.info("Settings update: authority JWT secret saved (len=%d)", len(body.authority_jwt_secret))

    if body.authority_hmac_secret:
        # Same policy as authority_jwt_secret above. This key drives the HMAC request
        # signing in `hmac_middleware`/`hmac_signer` — also HMAC-SHA256, also
        # only as strong as the secret behind it. Applying the rule to one of
        # the two HS256 secrets and not the other would leave an equivalent hole
        # open while letting the finding be marked closed.
        _weak = assess_hmac_secret(body.authority_hmac_secret, label="authority HMAC secret")
        if _weak:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The authority HMAC secret does not meet HMAC key-strength "
                    "policy (CVE-2025-45768 hardening): "
                    + "; ".join(_weak)
                    + ". This secret is issued by the authority — rotate it on "
                    "the authority's admin console to obtain a compliant value, "
                    "then paste that here."
                ),
            )
        _upsert(db, "authority_hmac_secret", body.authority_hmac_secret)
        persisted.append("authority_hmac_secret")
        logger.info("Settings update: authority HMAC secret saved (len=%d)", len(body.authority_hmac_secret))

    # CERT-4: validated on WRITE. The cert_config_request handler falls back
    # to the demo profile on malformed JSON (an inbound A2A reply is no place
    # to error), which means a bad value stored HERE would silently certify
    # the partner against demo values — so this is where it fails loudly.
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
        from app.authority_client import _is_private_url, _validate_url_scheme

        _validate_url_scheme(body.cert_trigger_url, purpose="certification trigger")

        # SSRF (SAST F-002): scheme/netloc presence is not a destination check.
        # Every inbound `cert_execution_start` auto-dispatches to this address
        # carrying the cert_trigger_secret bearer token — with no human in the
        # loop — so a stored `http://169.254.169.254/…` becomes a credentialed
        # request to the cloud metadata service on the authority's schedule. Reject at
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
                    "the host to AUTHORITY_SSRF_ALLOWED_HOSTS or by setting "
                    "AUTHORITY_SSRF_ALLOW_PRIVATE_NETWORKS=true. Nothing was saved."
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

    # Record WHO. `user` was declared on this handler and then never read, so
    # rotating `authority_jwt_secret` or `authority_hmac_secret` — the keys
    # that authenticate the entire A2A ingress — produced a log line that
    # could not answer "who changed it". The count of secret-bearing fields
    # is emitted rather than their names, keeping the taint tracker's
    # objection satisfied while still distinguishing a credential rotation
    # from a URL edit.
    rotated = sum(1 for k in persisted if k in SECRET_KEYS)
    if rotated:
        from app.core.security_events import emit_security_event
        emit_security_event(
            event_name="credential_rotated",
            severity="medium",
            boundary="dashboard_settings",
            decision="accepted",
            reason_code=f"secret_fields={rotated}",
            actor=user.username,
        )
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
def check_authority_connectivity(
    body: TestConnectionRequest | None = None,
    user: PartnerUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # If the typed value contains "****" it's the masked display the
    # operator copied off the authority's admin Partners list (mask format is
    # <8>****<4>). Real api_keys are token_urlsafe — they can't contain
    # asterisks — so this check has no false positives. Fail fast with
    # a specific message instead of silently falling through to the DB
    # (which yields the misleading "Partner API Key is not configured"
    # toast even though the operator clearly pasted something).
    if body and body.api_key and "****" in body.api_key:
        return {
            "status": "error",
            "message": "You pasted the masked display (e.g. 'a2a_xxxx****xxxx'), "
                       "not the real API key. The authority shows the real value "
                       "only ONCE at partner-create or after Rotate Key. Click "
                       "'Rotate Key' on the authority's admin Partners page to get "
                       "the full value, then paste THAT here.",
        }

    api_key_override = body.api_key if (body and body.api_key) else None
    return run_authority_reachability_check(db, api_key_override=api_key_override)


def _upsert(db: Session, key: str, value: str):
    value = value.strip()
    if key in SECRET_KEYS and value:
        # Encrypt at rest —
        # Fernet via core/secret_box.py, the same scheme and `enc:v1:` format
        # the authority backend uses. Fails loudly if PARTNER_SECRET_KEK is
        # unset or unusable, rather than silently persisting plaintext.
        #
        # `encrypt()` is a no-op on an already-encrypted value, so a re-save
        # cannot double-wrap it into an unrecoverable state.
        try:
            value = encrypt(value)
        except SecretBoxError as exc:
            # This used to propagate as a bare 500 {"detail":"An internal error
            # occurred"}, which told an admin mid-credential-install nothing at
            # all — the actual cause (a KEK decoding to the wrong length) was
            # reachable only by reading container logs. Naming the variable
            # discloses nothing: this endpoint is already require_admin, and the
            # operator can read their own environment. The KEK VALUE is never
            # included — `exc` carries the decoded length, not the key. F-25.
            #
            # 503, not 500: the request was well-formed and the server is
            # misconfigured, which is the same shape as the A2A middleware's
            # fail-closed 503. Nothing is persisted — the handler's single
            # db.commit() has not run, so the whole PUT stays atomic.
            logger.error("Settings save failed: secret encryption unavailable (%s)", exc)
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cannot encrypt secrets: {exc} Nothing was saved. This is a "
                    f"server configuration problem, not a problem with the value "
                    f"you submitted — fix PARTNER_SECRET_KEK in the backend "
                    f"environment and restart the backend, then save again."
                ),
            ) from exc
    row = db.get(PartnerSetting, key)
    if row:
        row.value = value
    else:
        db.add(PartnerSetting(key=key, value=value))


def _mask(value: str) -> str:
    """A recognisable stub, not a preview.

    This used to return `value[:8] + "****" + value[-4:]` — twelve plaintext
    characters of, among others, the HS256 keys that gate the entire A2A
    ingress, handed back by GET /api/settings. The UI needs two things from
    this: to show that a value IS set (the `has_*` flags alongside already do
    that), and to let an operator recognise WHICH credential is installed. Four
    trailing characters is enough to tell two keys apart; the leading eight
    were the identifiable part of several of these formats (`a2a_`, `sk-ant-`,
    `glpat-`) and bought nothing the prefix-free stub does not.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return "****" + value[-4:]
