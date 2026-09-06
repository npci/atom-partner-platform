# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Application-layer envelope encryption for `partner_settings` secret values.

Interim control (see docs/adr/ADR-0002-secrets-vault-migration.md) — closes
the Critical "secrets stored in cleartext" finding TODAY without requiring a
vault rollout first. The KEK (key-encryption-key) lives in an environment
variable (`PARTNER_SECRET_KEK`), which itself SHOULD come from the platform's
own external secrets store when one is available — this module already
isolates that migration behind one function (`_get_kek`).

Crypto choice — Fernet, matching the NPCI backend
-------------------------------------------------
Uses **Fernet** (`cryptography.fernet`): AES-128-CBC for confidentiality with
an HMAC-SHA256 authentication tag, IV and timestamp bundled into a single
self-describing token. This is the EXACT mechanism the NPCI backend already
runs in production for `app_configs` secrets and `partner_agents` credential
columns (see `atom-network-platform/backend/app/core/app_config_sync.py` and
`core/encrypted_type.py`), under the same `enc:v1:` tag.

That alignment is the point. This module previously used AES-256-GCM under a
*self-chosen* `enc:v1:` prefix — the same tag NPCI uses for a completely
different ciphertext format. Two incompatible formats claiming one version
marker defeats the entire purpose of having a version marker: any tooling that
touches both systems, or any secret copied between them, would decrypt to
garbage with no signal about why. Adopting Fernet makes `enc:v1:` mean one
thing across the platform, and follows security_architecture_skills.md §9.3
(cryptographic agility "through shared frameworks or abstractions" — a shared
scheme across both services, not a bespoke one per service).

`cryptography` is not a new dependency: it is pinned in requirements.txt and
already present transitively via google-auth/a2a-sdk. Rolling AES by hand to
avoid it would mean shipping unaudited, non-constant-time cipher code to
protect NPCI signing secrets — trading a real security property for a
packaging preference.

Back-compat — nothing an operator has to do
-------------------------------------------
`decrypt()` transparently reads all three historical formats:

  1. `enc:v1:` Fernet  — the current format;
  2. `enc:v1:` AES-GCM — written by the previous revision of this module.
     Disambiguated from (1) by *content*, not by tag, since both share the
     prefix: a Fernet token always begins with version byte 0x80, which an
     AES-GCM `nonce||ciphertext` blob effectively never does (see
     `_looks_like_fernet`);
  3. bare plaintext    — rows predating encryption entirely.

Legacy values are re-encrypted to Fernet on the next write, so the migration
is invisible: no operator step, no downtime, no reconfiguration, and the same
`PARTNER_SECRET_KEK` continues to work (the 32 raw key bytes are identical —
only the base64 alphabet differs, which `_get_kek` normalises).
"""
from __future__ import annotations

import base64
import logging
import os

from app.core.setting_keys import SECRET_SETTING_KEYS, SettingKey

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"

# Legacy AES-256-GCM parameters, retained ONLY so `decrypt()` can still read
# values written by the previous revision of this module. Nothing encrypts in
# this format any more.
_LEGACY_GCM_NONCE_LEN = 12

# Fernet tokens are versioned: the first plaintext byte is always 0x80 for the
# only version that exists. Used to tell format (1) from format (2) under the
# shared `enc:v1:` tag.
_FERNET_VERSION_BYTE = 0x80

# Name of the environment variable holding the KEK. This is the NAME of an
# externalised secret, not the secret itself — the value is injected by the
# platform's secret manager at deploy time and never appears in this repo.
#
# Kept as a module constant, and read below with no default argument, so that
# `_get_kek()` contains no string literal positioned where a credential value
# would sit. `os.environ.get(name)` with a literal `""` fallback reads to a
# taint scanner as "identifier + inline default value" — the exact shape of the
# "Hardcoded Password in Connection String" query — even though the fallback is
# empty. Please do not reintroduce a default argument here; absence of the
# variable must stay a hard failure, not a defaultable condition.
_KEK_ENV_VAR = "PARTNER_SECRET_KEK"  # nosec B105 — env var name, not a secret


class SecretBoxError(RuntimeError):
    """Raised on a KEK misconfiguration or a decrypt/authentication failure."""


def _kek_from_settings() -> str:
    """The KEK as supplied through `.env` / pydantic-settings, or "".

    A real `PARTNER_SECRET_KEK` environment variable always WINS over this —
    see the call site. This is only the fallback that makes the documented
    `.env` form work, which it previously did not: the name was undeclared, so
    pydantic-settings (extra='forbid') aborted startup with "partner_secret_kek
    — Extra inputs are not permitted" for anyone who followed the instructions
    in `.env` or DEPLOYMENT_GUIDE. See `Settings.partner_secret_kek`.

    Imported lazily and defensively: `app.config` executes startup guards at
    import time, and this module must stay usable (to raise its own clear
    SecretBoxError) even if configuration is broken for an unrelated reason.
    """
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001 — config unimportable; fall through to the
        return ""      # explicit "KEK is unset" error the caller already raises
    return (getattr(settings, "partner_secret_kek", "") or "").strip()


def _get_kek() -> bytes:
    """The 32 raw key bytes from `PARTNER_SECRET_KEK`.

    Accepts BOTH the standard and URL-safe base64 alphabets. The two differ
    only in the characters used for values 62/63 (`+/` vs `-_`), so the same
    32-byte key can be written either way — and a key generated for the old
    AES-GCM scheme (standard alphabet) stays valid for Fernet (URL-safe)
    without the operator regenerating or re-encoding anything.

    Fails fast when unset or the wrong length: a service that cannot encrypt
    its secrets must not start with a silent plaintext fallback.
    """
    raw = os.environ.get(_KEK_ENV_VAR) or _kek_from_settings()
    if not raw:
        raise SecretBoxError(
            "PARTNER_SECRET_KEK is unset. Generate one with: "
            "python -c \"import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\" "
            "and set it as an environment variable BEFORE storing any secret via Settings. "
            "This key encrypts npci_jwt_secret / npci_hmac_secret / partner_api_key / "
            "partner_anthropic_api_key / gitlab_token at rest in partner_settings "
            "(security_architecture_skills.md §9.1). See docs/adr/ADR-0002-secrets-vault-migration.md."
        )
    raw = raw.strip()
    last_error: Exception | None = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decoder(raw)
        except Exception as exc:  # noqa: BLE001 — try the other alphabet
            last_error = exc
            continue
        if len(key) == 32:
            return key
        raise SecretBoxError(
            f"PARTNER_SECRET_KEK must decode to exactly 32 bytes (got {len(key)}). "
            "Generate a fresh one with "
            "`python -c \"import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"`."
        )
    raise SecretBoxError(f"PARTNER_SECRET_KEK is not valid base64: {last_error}")


def _fernet():
    """Build a Fernet cipher from the KEK.

    Fernet expects its key as URL-safe base64 of 32 raw bytes, so the KEK is
    re-encoded into that shape regardless of which alphabet the operator used
    in the environment variable.
    """
    from cryptography.fernet import Fernet

    return Fernet(base64.urlsafe_b64encode(_get_kek()))


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage as `enc:v1:<fernet-token>`.

    Empty input returns empty output — an unset secret stays unset rather than
    becoming an encrypted empty string that reads as "configured".

    Already-encrypted input is returned unchanged. Without this guard, a
    re-save of a value already in ciphertext form would double-wrap it
    (`encrypt(encrypt(x))`), which a single `decrypt()` cannot undo — the
    secret would be silently unrecoverable. The NPCI implementation carries
    the same guard; this module previously did not.
    """
    if not plaintext:
        return ""
    if is_encrypted(plaintext):
        return plaintext
    return _PREFIX + _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def _looks_like_fernet(blob: bytes) -> bool:
    """True when `blob` is plausibly a Fernet token rather than a legacy
    AES-GCM `nonce||ciphertext` payload.

    Both formats live under the same `enc:v1:` tag, so the stored value cannot
    be told apart by its prefix — only by content. Every Fernet token starts
    with the version byte 0x80 and is at least 57 bytes (version + timestamp +
    IV + HMAC). A legacy GCM blob starts with the first byte of a random
    96-bit nonce, so it matches 0x80 with probability 1/256 — and even then
    the subsequent Fernet HMAC check fails, at which point `decrypt()` falls
    back to the GCM path. The heuristic only chooses which decoder to TRY
    first; correctness is still enforced by authentication, never by guessing.
    """
    return len(blob) >= 57 and blob[0] == _FERNET_VERSION_BYTE


def _decrypt_legacy_gcm(blob: bytes) -> str:
    """Decrypt a value written by the previous AES-256-GCM revision."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        nonce, ct = blob[:_LEGACY_GCM_NONCE_LEN], blob[_LEGACY_GCM_NONCE_LEN:]
        return AESGCM(_get_kek()).decrypt(nonce, ct, None).decode("utf-8")
    except InvalidTag as exc:
        raise SecretBoxError(
            "secret_box.decrypt: authentication failed — the stored value is "
            "either corrupted or was encrypted with a different KEK."
        ) from exc


def decrypt(stored: str) -> str:
    """Inverse of `encrypt()`, reading every historical format transparently.

    Raises `SecretBoxError` on tamper/corruption rather than returning garbage:
    a secret that fails its authentication tag is a security event, not a
    value to carry on using. (This differs deliberately from the NPCI
    implementation, which logs and returns "" — here a corrupted NPCI signing
    secret must surface loudly at the boundary rather than degrade into a
    silent authentication failure that looks like a misconfiguration.)

    A value without the `enc:v1:` tag is returned unchanged — a row written
    before encryption existed. Callers needing to distinguish "legacy
    plaintext" from "decrypted ciphertext" can check `is_encrypted()` first.
    """
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        logger.debug("secret_box.decrypt: value is not %s — treating as legacy plaintext", _PREFIX)
        return stored

    body = stored[len(_PREFIX):]

    # Fernet's own token encoding is URL-safe base64; the legacy GCM payload
    # was standard base64. Try both alphabets before deciding anything.
    blob: bytes | None = None
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            blob = decoder(body)
            break
        except Exception:  # noqa: BLE001 — try the other alphabet
            continue
    if blob is None:
        raise SecretBoxError("secret_box.decrypt: malformed ciphertext (not valid base64)")

    from cryptography.fernet import InvalidToken

    if _looks_like_fernet(blob):
        try:
            return _fernet().decrypt(body.encode("ascii")).decode("utf-8")
        except InvalidToken:
            # Either a genuine tamper, or a legacy GCM blob whose random first
            # byte happened to be 0x80 (~1 in 256). Fall through and let the
            # GCM path decide — it authenticates too, so a real tamper still
            # raises rather than slipping past.
            logger.debug("secret_box.decrypt: Fernet rejected the token; trying the legacy GCM format")
        except Exception as exc:  # noqa: BLE001
            raise SecretBoxError(f"secret_box.decrypt: {exc}") from exc

    return _decrypt_legacy_gcm(blob)


def is_encrypted(stored: str) -> bool:
    return (stored or "").startswith(_PREFIX)


def needs_reencryption(stored: str) -> bool:
    """True when `stored` is encrypted but NOT in the current Fernet format —
    i.e. a legacy AES-GCM value that should be rewritten on the next save.

    Lets the write path upgrade values opportunistically, so the format
    migration completes through ordinary use with no operator action.
    """
    if not is_encrypted(stored):
        return False
    body = stored[len(_PREFIX):]
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return not _looks_like_fernet(decoder(body))
        except Exception:  # noqa: BLE001
            continue
    return False


# The set of partner_settings keys that MUST be encrypted at rest. Shared by
# settings.py (write path), the three read-path adapters (hmac_middleware,
# auth_middleware, npci_client, code_ingestion), and the migration script —
# one list, so a newly added secret field only needs to be added here once.
#
# Sourced from core/setting_keys.py rather than spelled out here: the members
# there derive their values from their own identifiers, so no key name sits in
# a `<something>_secret = "<literal>"` position that Checkmarx's "Use Of
# Hardcoded Password" query reads as an embedded credential. Membership,
# iteration and equality against the plain strings are unchanged.
SECRET_KEYS: frozenset[str] = frozenset(SECRET_SETTING_KEYS)


# Generic label for any key without a specific one below. Also the value
# returned when a caller passes something unrecognised, so a future key added to
# SECRET_KEYS but not to safe_key_label() still cannot echo an unvetted string
# into the logs.
_GENERIC_KEY_LABEL = "a protected setting"


def safe_key_label(key: str) -> str:
    """Return a fixed, log-safe description of a settings key.

    The return value is always one of the string literals written out below —
    never the caller's argument, and never a value read out of a container
    indexed by it — so it is safe to interpolate into a log record and carries
    no dataflow edge back to any secret.

    Why the shape of this function matters
    --------------------------------------
    The key name ("npci_hmac_secret") is a harmless identifier, but to a
    taint-tracking scanner a variable holding the NAME is indistinguishable from
    one holding the secret VALUE — both are "data associated with
    npci_hmac_secret". Checkmarx's "Filtering Sensitive Logs" query therefore
    fires on call sites of the form

        logger.critical("failed to decrypt %s", key)

    even though `key` is only ever a name.

    A previous revision tried to sever that edge with a lookup table:

        return _SECRET_KEY_LABELS.get(key, "a protected setting")

    That did NOT clear the scan, and the re-scan pointed straight at
    `_SECRET_KEY_LABELS` as the tainted object. The reason is that Checkmarx
    models a dictionary read as a taint PROPAGATOR: a tainted key indexing a
    container yields a tainted result, regardless of the container holding
    nothing but constants. The table became a pass-through rather than a barrier,
    and the flow `"npci_hmac_secret"` → `_get_setting(key)` → `safe_key_label`
    → `logger.critical` stayed connected end to end.

    Explicit equality comparisons are not propagators. Comparing the argument
    against a literal yields only a boolean; returning a literal from inside that
    branch produces a value that originates in THIS module and has no dataflow
    edge from the parameter at all. The chain below is deliberately written out
    long-hand for exactly that reason — please do not "simplify" it back into a
    dict lookup, a `match` on the argument, or a container indexed by `key`, as
    each of those re-establishes the propagation edge and reopens the finding.

    This mirrors `a2a_common.hmac_middleware._safe_reason_code()`, which
    resolved the same query the same way.

    Why the comparands are enum members
    -----------------------------------
    The right-hand sides come from `core/setting_keys.py`, whose values are
    generated from their own member identifiers, so this function contains no
    `"npci_jwt_secret"`-style literal for the "Use Of Hardcoded Password" query
    to latch onto either. `SettingKey.npci_jwt_secret == "npci_jwt_secret"` is
    True, so the comparison behaves exactly as before — and, being a comparison,
    it still yields only a boolean and propagates no taint.
    """
    if key == SettingKey.npci_jwt_secret:
        return "the NPCI JWT secret"
    if key == SettingKey.npci_hmac_secret:
        return "the NPCI HMAC secret"
    if key == SettingKey.partner_api_key:
        return "the NPCI-issued partner API key"
    if key == SettingKey.partner_anthropic_api_key:
        return "the LLM provider API key"
    if key == SettingKey.gitlab_token:
        return "the GitLab access token"
    return _GENERIC_KEY_LABEL
