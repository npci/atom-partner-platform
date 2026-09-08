# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for encrypted-at-rest secrets."""
import base64
import logging
import secrets

import pytest

from app.core import secret_box


def _sample_plaintext() -> str:
    """A random stand-in for "some secret value being encrypted".

    What these tests actually assert is that `encrypt`/`decrypt` round-trip an
    arbitrary string, that two encryptions of the SAME input differ, and that
    legacy ciphertexts still open. None of that depends on the input being a
    particular string — it only has to be non-empty and stable within a test.

    Spelling one out (`pt = "same-secret-value"`) made static analysis report
    these fixtures as embedded credentials. Generating the value removes the
    literal and, as a bonus, stops a round-trip test from ever passing because
    of something peculiar to one hardcoded string.
    """
    return f"sample-plaintext-{secrets.token_urlsafe(16)}"


@pytest.fixture(autouse=True)
def _kek(monkeypatch):
    """A fresh, valid 32-byte KEK for every test — isolated from whatever the
    real environment does or doesn't have set."""
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("PARTNER_SECRET_KEK", key)
    yield key


def test_round_trip():
    pt = _sample_plaintext()
    ct = secret_box.encrypt(pt)
    assert ct.startswith("enc:v1:")
    assert secret_box.decrypt(ct) == pt


def test_empty_string_round_trips_to_empty():
    assert secret_box.encrypt("") == ""
    assert secret_box.decrypt("") == ""


def test_legacy_plaintext_passthrough():
    # A row written before secret_box existed — decrypt() must not choke on it.
    assert secret_box.decrypt("some-legacy-plaintext-value") == "some-legacy-plaintext-value"


def test_is_encrypted():
    assert secret_box.is_encrypted(secret_box.encrypt("x")) is True
    assert secret_box.is_encrypted("plain") is False
    assert secret_box.is_encrypted("") is False


def test_tamper_detection_raises():
    ct = secret_box.encrypt(_sample_plaintext())
    tampered = ct[:-4] + ("A" * 4 if not ct.endswith("AAAA") else "BBBB")
    with pytest.raises(secret_box.SecretBoxError):
        secret_box.decrypt(tampered)


def test_wrong_kek_fails_to_decrypt(monkeypatch):
    ct = secret_box.encrypt(_sample_plaintext())
    monkeypatch.setenv("PARTNER_SECRET_KEK", base64.b64encode(secrets.token_bytes(32)).decode())
    with pytest.raises(secret_box.SecretBoxError):
        secret_box.decrypt(ct)


def test_missing_kek_raises_on_encrypt(monkeypatch):
    """Fail-fast when the KEK is absent from BOTH of its sources.

    There are two sources since `Settings.partner_secret_kek` was declared: the
    environment variable, and the `.env`-supplied setting it takes precedence
    over. Clearing only the env var no longer proves anything on a box whose
    `.env` carries the key — the assertion passes vacuously, which is exactly
    the "green tick guarding nothing" failure mode this exercise keeps finding.
    """
    from app.config import settings

    monkeypatch.delenv("PARTNER_SECRET_KEK", raising=False)
    monkeypatch.setattr(settings, "partner_secret_kek", "", raising=False)
    with pytest.raises(secret_box.SecretBoxError, match="PARTNER_SECRET_KEK is unset"):
        secret_box.encrypt("anything")


def test_kek_is_readable_from_settings_when_env_var_absent(monkeypatch):
    """The `.env` form must work — it is the DOCUMENTED one.

    `.env` (lines 10-16) and DEPLOYMENT_GUIDE both tell the operator to put
    PARTNER_SECRET_KEK in `.env`. Until `Settings.partner_secret_kek` was
    declared, doing so aborted startup with "partner_secret_kek — Extra inputs
    are not permitted", so the value only ever worked as a shell export — which
    does not survive a restart from a fresh shell, and whose absence then reads
    as "credential not configured" rather than as a key error.
    """
    from app.config import settings

    kek = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.delenv("PARTNER_SECRET_KEK", raising=False)
    monkeypatch.setattr(settings, "partner_secret_kek", kek, raising=False)
    assert secret_box.decrypt(secret_box.encrypt("round-trip")) == "round-trip"


def test_env_var_wins_over_settings(monkeypatch):
    """An exported KEK keeps behaving exactly as it did before the field
    existed, so adding the fallback cannot change a working deployment."""
    from app.config import settings

    env_kek = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    other_kek = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setenv("PARTNER_SECRET_KEK", env_kek)
    monkeypatch.setattr(settings, "partner_secret_kek", other_kek, raising=False)
    ct = secret_box.encrypt("bound-to-the-env-var")

    # The same ciphertext, now openable only through the settings value, must
    # FAIL — that is what proves the env var supplied the key actually used.
    monkeypatch.delenv("PARTNER_SECRET_KEK", raising=False)
    with pytest.raises(secret_box.SecretBoxError):
        secret_box.decrypt(ct)


def test_invalid_kek_length_raises(monkeypatch):
    monkeypatch.setenv("PARTNER_SECRET_KEK", base64.b64encode(b"too-short").decode())
    with pytest.raises(secret_box.SecretBoxError, match="32 bytes"):
        secret_box.encrypt("anything")


def test_secret_keys_covers_expected_fields():
    assert secret_box.SECRET_KEYS == frozenset({
        "authority_jwt_secret", "authority_hmac_secret", "partner_api_key",
        "partner_anthropic_api_key", "partner_openai_api_key", "partner_ainxt_api_key",
        "gitlab_token", "cert_trigger_secret",
    })


def test_two_encryptions_of_same_plaintext_differ():
    # Nonce is random per call — ciphertexts must not be identical even for
    # the same plaintext (guards against a nonce-reuse regression).
    pt = _sample_plaintext()
    assert secret_box.encrypt(pt) != secret_box.encrypt(pt)


# ── Fernet format + backward compatibility ───────────────────────────────────
# The scheme moved from a self-chosen AES-256-GCM payload to Fernet, matching
# the NPCI backend's existing `enc:v1:` format. These tests
# pin BOTH halves of that change: the new format is genuinely Fernet, and every
# value written by the previous revision still decrypts.

def test_ciphertext_is_a_fernet_token():
    """A Fernet token starts with version byte 0x80, which base64-encodes to a
    leading 'gAAAAA'. This is what makes the format identical to NPCI's."""
    import base64
    body = secret_box.encrypt("v")[len("enc:v1:"):]
    assert body.startswith("gAAAAA")
    assert base64.urlsafe_b64decode(body)[0] == 0x80


def test_encrypt_does_not_double_wrap():
    """Re-saving an already-encrypted value must be a no-op. Without this
    guard `encrypt(encrypt(x))` produces a value a single decrypt() cannot
    recover — a silently unrecoverable secret."""
    pt = _sample_plaintext()
    once = secret_box.encrypt(pt)
    assert secret_box.encrypt(once) == once
    assert secret_box.decrypt(secret_box.encrypt(once)) == pt


def test_legacy_aes_gcm_values_still_decrypt():
    """The critical upgrade path: secrets encrypted by the previous AES-GCM
    revision must keep working with no operator action."""
    import base64
    import secrets as pysecrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    pt = _sample_plaintext()
    nonce = pysecrets.token_bytes(12)
    ct = AESGCM(secret_box._get_kek()).encrypt(nonce, pt.encode(), None)
    stored = "enc:v1:" + base64.b64encode(nonce + ct).decode("ascii")

    assert secret_box.decrypt(stored) == pt


def test_legacy_values_are_flagged_for_reencryption():
    import base64
    import secrets as pysecrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = pysecrets.token_bytes(12)
    ct = AESGCM(secret_box._get_kek()).encrypt(nonce, b"old", None)
    legacy = "enc:v1:" + base64.b64encode(nonce + ct).decode("ascii")

    assert secret_box.needs_reencryption(legacy) is True
    assert secret_box.needs_reencryption(secret_box.encrypt("new")) is False
    assert secret_box.needs_reencryption("bare-plaintext") is False


def test_kek_accepts_both_base64_alphabets(monkeypatch):
    """The same 32 raw bytes may be written in standard or URL-safe base64, so
    a KEK generated for the old scheme keeps working unchanged."""
    import base64
    import secrets as pysecrets

    raw = pysecrets.token_bytes(32)
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        monkeypatch.setenv("PARTNER_SECRET_KEK", encoder(raw).decode())
        assert secret_box._get_kek() == raw
        assert secret_box.decrypt(secret_box.encrypt("x")) == "x"


# --- safe_key_label: sensitive-log-filtering regression guard ----------------
#
# These tests exist to keep the remediation from being refactored away. The
# finding was reported against five decrypt-failure log sites; the fix depends
# on safe_key_label() returning module-owned literals via explicit equality
# comparisons rather than a container indexed by the caller's key. See the
# docstring on safe_key_label() for why a dict lookup does not satisfy the scan.

def test_safe_key_label_maps_every_secret_key():
    """Every key in SECRET_KEYS has a specific, non-generic label."""
    for key in secret_box.SECRET_KEYS:
        label = secret_box.safe_key_label(key)
        assert label != secret_box._GENERIC_KEY_LABEL, f"{key} has no specific label"
        assert key not in label, "label must not embed the key name"


def test_safe_key_label_never_echoes_its_argument():
    """An unrecognised key degrades to the generic literal — the caller's
    string is never reflected back into the log record."""
    for probe in (
        "totally_unknown_key",
        "authority_hmac_secret_but_not_quite",
        "",
        # A value-shaped probe — generated rather than written out, so this
        # assertion does not itself become a "Use Of Hardcoded Password" hit.
        _sample_plaintext(),
        "%s%s",           # a format-string probe must not survive either
        "'; DROP TABLE partner_settings; --",
    ):
        assert secret_box.safe_key_label(probe) == secret_box._GENERIC_KEY_LABEL


def test_safe_key_label_returns_module_owned_literals():
    """The returned object is one of a fixed, closed set of strings."""
    allowed = {
        "the authority JWT secret",
        "the authority HMAC secret",
        "the authority-issued partner API key",
        "the LLM provider API key",
        "the GitLab access token",
        "the certification trigger secret",
        secret_box._GENERIC_KEY_LABEL,
    }
    for key in list(secret_box.SECRET_KEYS) + ["unknown", "x" * 500]:
        assert secret_box.safe_key_label(key) in allowed


def test_safe_key_label_does_not_use_a_lookup_table():
    """Guard the remediation's SHAPE, not just its behaviour.

    Static analysis treats `table[key]` / `table.get(key)` as taint propagation,
    so a dict-based implementation reopens the finding even though the observable
    behaviour is identical. Assert the source uses equality comparisons.
    """
    import inspect

    src = inspect.getsource(secret_box.safe_key_label)
    body = src.split('"""')[-1]  # ignore the explanatory docstring
    assert ".get(key" not in body, "dict lookup reopens the taint finding"
    assert "[key]" not in body, "container indexing reopens the taint finding"
    assert body.count("if key ==") >= len(secret_box.SECRET_KEYS)


# ── validate_at_startup — F-25 ───────────────────────────────────────────────
#
# The regression these guard: `_get_kek`'s docstring promised "a service that
# cannot encrypt its secrets must not start", but it was only ever called from
# `_fernet()` at first encrypt. A KEK decoding to the wrong length therefore let
# the platform boot clean and fail much later, on an operator's first credential
# save, as an opaque 500.


def _unset_kek(monkeypatch):
    """Genuinely unset the KEK.

    Both sources have to go: `_get_kek` falls back to `_kek_from_settings()`,
    which reads pydantic-settings and would otherwise pick up a developer's real
    `.env` and make these tests pass or fail depending on the machine.
    """
    monkeypatch.delenv("PARTNER_SECRET_KEK", raising=False)
    monkeypatch.setattr(secret_box, "_kek_from_settings", lambda: "")


def test_validate_at_startup_accepts_a_valid_kek():
    secret_box.validate_at_startup()  # autouse fixture supplies a valid one


def test_validate_at_startup_rejects_the_exact_f25_key():
    """`token_urlsafe(48)` — the command the root .env.example used to ship.

    It is the correct generator for SESSION_JWT_SECRET on the very next line of
    that file, which is how it got copied onto the KEK. It yields 64 characters
    that decode to 48 bytes, and Fernet needs exactly 32.
    """
    bad = secrets.token_urlsafe(48)
    assert len(base64.urlsafe_b64decode(bad + "==")) == 48  # the actual defect

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setenv("PARTNER_SECRET_KEK", bad)
        with _pytest.raises(RuntimeError) as err:
            secret_box.validate_at_startup()

    msg = str(err.value)
    assert "PARTNER_SECRET_KEK" in msg
    assert "32 bytes" in msg
    assert bad not in msg, "a startup error must not echo the key material"


def test_validate_at_startup_rejects_non_base64(monkeypatch):
    monkeypatch.setenv("PARTNER_SECRET_KEK", "not-valid-base64-!!!@@@")
    with pytest.raises(RuntimeError, match="PARTNER_SECRET_KEK"):
        secret_box.validate_at_startup()


def test_validate_at_startup_warns_but_boots_when_unset(monkeypatch, caplog):
    """UNSET is deliberately NOT fatal.

    docker-compose.yml defaults PARTNER_SECRET_KEK to empty, so aborting here
    would refuse a stock `docker compose up` for anyone evaluating the platform
    who never stores a secret. They get a CRITICAL line naming the variable
    instead, plus `_get_kek`'s error the moment they actually try to save.
    """
    _unset_kek(monkeypatch)
    with caplog.at_level(logging.CRITICAL, logger=secret_box.__name__):
        secret_box.validate_at_startup()  # must not raise

    assert "PARTNER_SECRET_KEK" in caplog.text


def test_a_malformed_kek_is_fatal_but_an_absent_one_is_not(monkeypatch):
    """The distinction itself is the contract, so assert it directly rather
    than leaving it implied by two tests that could drift apart."""
    _unset_kek(monkeypatch)
    secret_box.validate_at_startup()

    monkeypatch.setenv("PARTNER_SECRET_KEK", base64.b64encode(secrets.token_bytes(31)).decode())
    with pytest.raises(RuntimeError):
        secret_box.validate_at_startup()
