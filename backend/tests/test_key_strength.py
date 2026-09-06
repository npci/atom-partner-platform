# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for HMAC key-strength policy (CVE-2025-45768 hardening).

This is the enforcement half of the `pyjwt` SBOM annotation. The pinned PyJWT
(2.13.0) already emits `InsecureKeyLengthWarning` for a short HMAC key; this
application escalates that to a hard failure, applies it before a secret is
persisted rather than at signing time, and additionally rejects keys that are
long enough but guessable. These tests keep all three properties true.

TWO FAILURE MODES, BOTH TESTED — AND THE SECOND IS THE DANGEROUS ONE
--------------------------------------------------------------------
1. FALSE NEGATIVES — a weak key slips through. That is the CVE condition,
   unfixed, with an annotation claiming otherwise.

2. FALSE POSITIVES — a legitimately generated key is rejected. On this codebase
   that is arguably worse: `npci_jwt_secret` is issued by NPCI and validated on
   write, so a spurious rejection blocks real partner onboarding and teaches
   operators that the check is noise.

   This is not hypothetical. The FIRST implementation used a distinct-character
   floor of 10 and a per-character Shannon entropy floor of 3.0 bits. Measured
   consequences:

     - `secrets.token_hex(16)` — a documented, valid 32-byte key — was rejected
       at 0.0153% (61 of 400,000 samples). The suite that guarded this asserted
       zero rejections over 2,000 samples, giving a 26% chance of a spurious
       failure per run. It failed 5 of 12 consecutive runs.
     - 40-character numeric keys were rejected at 15.3%.
     - Meanwhile six genuinely guessable values PASSED, including
       "NPCIPartnerPlatform2026SigningKey01" and sha256("password").

   The metrics were not merely mistuned, they were anti-correlated with the
   goal: they measure character-frequency flatness, while guessability is
   structural. `test_metrics_cannot_separate_by_frequency_alone` pins that
   finding so the old approach is not reintroduced.

The current implementation measures total entropy plus structure (dictionary
words, known digests, repetition, placeholders) and scores 0 false positives
across 540,000 generated keys with 0 false negatives on the weak corpus.
"""
from __future__ import annotations

import base64
import math
import secrets
import string
import uuid

import pytest

from app.core.key_strength import (
    MAX_ABSOLUTE_RUN,
    MIN_HMAC_KEY_BYTES,
    MIN_PADDING_RUN,
    MIN_TOTAL_ENTROPY_BITS,
    WeakKeyError,
    _PLACEHOLDER_TOKENS,
    _longest_run,
    assess_hmac_secret,
    generation_hint,
    require_strong_hmac_secret,
)

# ── Generators that model how a real secret is produced ─────────────────────
# Every one is a legitimate way to make an HS256 key and MUST pass. The list
# deliberately extends beyond `secrets` output to institutional formats an
# NPCI-issued key might plausibly arrive in — UUID, grouped hex, prefixed,
# base32, numeric — because those are what a false positive would block.
_GOOD_GENERATORS = {
    "token_urlsafe(48) [documented]": lambda: secrets.token_urlsafe(48),
    "token_urlsafe(32)": lambda: secrets.token_urlsafe(32),
    "token_urlsafe(24)": lambda: secrets.token_urlsafe(24),
    "token_hex(32)": lambda: secrets.token_hex(32),
    # 32 hex chars = exactly at the byte floor, and the format that broke the
    # original implementation. Kept first among the risky cases on purpose.
    "token_hex(16) [at floor; broke v1]": lambda: secrets.token_hex(16),
    "b64(32 bytes)": lambda: base64.b64encode(secrets.token_bytes(32)).decode(),
    "b64(48 bytes)": lambda: base64.b64encode(secrets.token_bytes(48)).decode(),
    "b64url(32 bytes)": lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    "b64 with '=' padding": lambda: base64.b64encode(secrets.token_bytes(49)).decode(),
    "uuid4": lambda: str(uuid.uuid4()),
    "uuid4 doubled": lambda: str(uuid.uuid4()) + str(uuid.uuid4()),
    "UPPERCASE hex 64": lambda: secrets.token_hex(32).upper(),
    "grouped hex (XXXX-XXXX-...)": lambda: "-".join(
        secrets.token_hex(2).upper() for _ in range(8)
    ),
    "prefixed (npci_live_ + b64url)": lambda: "npci_live_" + base64.urlsafe_b64encode(
        secrets.token_bytes(24)
    ).decode().rstrip("="),
    "base32 (40 chars)": lambda: base64.b32encode(secrets.token_bytes(25)).decode(),
    "numeric 40 [broke v1 at 15%]": lambda: "".join(
        secrets.choice(string.digits) for _ in range(40)
    ),
    "numeric 32": lambda: "".join(secrets.choice(string.digits) for _ in range(32)),
    "alnum 32 mixed case": lambda: "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
    ),
}

# ── Values that MUST be rejected ────────────────────────────────────────────
# Grouped by the rule that should catch each one, so a failure points at the
# specific check that regressed.
_WEAK_SECRETS = {
    # length
    "empty": "",
    "tiny": "abc",
    "the word secret": "secret",
    "password": "password",
    "changeme": "changeme",
    "31 bytes (one under floor)": secrets.token_urlsafe(64)[:31],
    # repetition / low material
    "one char x32": "a" * 32,
    "one char x64": "a" * 64,
    "placeholder padded to length": "secret" + "a" * 40,
    "two chars alternating": "ab" * 20,
    "short unit tiled": "abcdefghij" * 4,
    "tiled with trailing char": "Monkey" * 6 + "X",
    "tiled with leading char": "X" + "Monkey" * 6,
    "leetspeak repeated": "P@ssw0rd!P@ssw0rd!P@ssw0rd!P@ssw0rd!",
    # dictionary structure — long, varied, and still guessable
    "passphrase": "Correct-Horse-Battery-Staple-Longer",
    "dictionary sentence": "the quick brown fox jumps over lazy",
    "company + year": "NPCIPartnerPlatform2026SigningKey01",
    "date based": "2026-08-28-partner-platform-hmac-key",
    "hostname based": "partner-backend-prod-01.npci.org.in",
    "infra hostname": "prod-kafka-broker-npci-mumbai-01",
    "domain phrase": "npci-upi-settlement-signing-key-2026",
    "shouty placeholder": "YOUR_SECRET_HERE_YOUR_SECRET_HERE",
    "template literal": "replace-me-with-a-real-secret-value",
    "realistic dev default": "insecure-dev-secret-please-change-me",
    "realistic named key": "my-jwt-secret-key-for-development-only",
    "project-flavoured": "partner-platform-secret-key-123456",
    # known digests — statistically perfect, cryptographically worthless
    "sha256('password')": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
    "md5('password')": "5f4dcc3b5aa765d61d8327deb882cf99",
    "sha1('123456')": "7c4a8d09ca3762af61e59520943dc26494f8941b",
    "sha256('password') UPPER": (
        "5E884898DA28047151D0E56F8DC6292773603D0D6AABBDD62A11EF721D1542D8"
    ),
    # placeholder tokens
    "digit sequence": "0123456789012345678901234567890123",
    "keyboard walk": "qwertyuiopasdfghjklzxcvbnm1234567890",
    "hyphenated placeholder": "changeme-changeme-changeme-changeme",
}

# Sample count per generator. 2,000 was enough to make the ORIGINAL bug fire
# 26% of the time; kept at 2,000 so a regression of similar magnitude is caught
# while the suite stays fast.
_FP_SAMPLES = 2000


@pytest.mark.parametrize(
    "name,value", sorted(_WEAK_SECRETS.items()),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_weak_secrets_are_rejected(name, value):
    """Every known-bad shape must produce at least one reason."""
    reasons = assess_hmac_secret(value, label="TEST_SECRET")
    assert reasons, (
        f"{name!r} was ACCEPTED but must be rejected — a guessable HMAC key "
        f"passing policy is the CVE-2025-45768 condition this module exists to "
        f"prevent"
    )
    # Reasons must be actionable prose, not a bare code.
    assert all(isinstance(r, str) and len(r) > 10 for r in reasons)


@pytest.mark.parametrize(
    "name,gen", sorted(_GOOD_GENERATORS.items()),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_no_false_positives_on_generated_keys(name, gen):
    """A CSPRNG-generated key must NEVER be rejected.

    This test caught a real, quantified bug — see the module docstring. Two
    rules to preserve its value:

      - Do NOT lower the sample count to make a failure go away. The heuristic
        being tripped is what is wrong.
      - Do NOT drop a generator from the list. `token_hex(16)` and the numeric
        formats are the ones that historically broke; removing them would hide
        exactly the regression this guards.
    """
    failures = []
    for _ in range(_FP_SAMPLES):
        reasons = assess_hmac_secret(gen())
        if reasons:
            # Record the reason, never the key material itself.
            failures.append(reasons[0])
    assert not failures, (
        f"{name} produced {len(failures)} false rejections in {_FP_SAMPLES} "
        f"samples ({len(failures) / _FP_SAMPLES:.3%}). A spurious rejection of "
        f"a valid NPCI-issued key blocks partner onboarding.\n"
        f"First reason: {failures[0]}"
    )


def test_metrics_cannot_separate_by_frequency_alone():
    """Pin the measurement that invalidated the original approach.

    Character-frequency metrics (distinct count, per-character Shannon entropy)
    rank several LEGITIMATE formats BELOW known-weak values. Any threshold on
    those axes therefore either rejects real keys or admits guessable ones —
    which is precisely what the first implementation did, in both directions.

    If this assertion ever fails, the sampling changed; it does not mean a
    frequency threshold has become viable.
    """
    def distinct(v):
        return len(set(v))

    def bits_per_char(v):
        counts = {}
        for ch in v:
            counts[ch] = counts.get(ch, 0) + 1
        n = len(v)
        return -sum((c / n) * math.log2(c / n) for c in counts.values())

    legit_hex = secrets.token_hex(16)                      # valid 32-byte key
    legit_digits = "".join(secrets.choice(string.digits) for _ in range(40))
    weak_words = "the quick brown fox jumps over lazy"     # guessable
    weak_hash = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"

    # The weak values score HIGHER on both metrics than the legitimate ones.
    assert distinct(weak_words) > distinct(legit_hex)
    assert distinct(weak_hash) > distinct(legit_digits)
    assert bits_per_char(weak_words) > bits_per_char(legit_digits)

    # Yet policy must reject the weak ones and accept the legitimate ones.
    assert assess_hmac_secret(legit_hex) == []
    assert assess_hmac_secret(legit_digits) == []
    assert assess_hmac_secret(weak_words)
    assert assess_hmac_secret(weak_hash)


def test_minimum_length_is_rfc7518_compliant():
    """The floor must be 32 bytes — RFC 7518 section 3.2 for HS256.

    Pinned so the constant cannot drift below the standard. This is also the
    threshold PyJWT's own InsecureKeyLengthWarning uses, so the two agree.
    """
    assert MIN_HMAC_KEY_BYTES == 32


def test_entropy_floor_is_below_every_legitimate_format():
    """The total-entropy floor must leave headroom under real key formats.

    Measured minimum across the generators above was ~81.6 bits (32 random
    digits). A floor at or above that would reintroduce false positives, which
    is the failure this whole recalibration addressed.
    """
    assert MIN_TOTAL_ENTROPY_BITS <= 80.0, (
        "the total-entropy floor has risen into the range occupied by "
        "legitimate numeric keys — false positives will return"
    )


def test_exactly_at_and_below_the_length_boundary():
    """Off-by-one check on the length floor, isolated from other heuristics.

    Uses random hex so the ONLY rule that can fire is length — otherwise a
    pass/fail here would not prove the boundary is where we think it is.
    """
    just_under = secrets.token_hex(32)[:31]
    reasons = assess_hmac_secret(just_under)
    assert any("31 bytes" in r for r in reasons)

    exactly_at = secrets.token_hex(16)  # 32 hex chars = 32 bytes UTF-8
    assert assess_hmac_secret(exactly_at) == []


def test_multibyte_secret_measured_in_bytes_not_characters():
    """Key length is measured in BYTES, matching what PyJWT signs.

    PyJWT UTF-8 encodes a `str` key before use, so a 20-character string of
    3-byte characters is a 60-byte key. Measuring characters would wrongly
    reject it. Guards against a future 'simplification' to len(value).
    """
    value = "".join(chr(0x4E00 + i) for i in range(20))
    assert len(value) == 20
    assert len(value.encode("utf-8")) == 60
    reasons = assess_hmac_secret(value)
    assert not any("bytes; HS256 requires" in r for r in reasons)


def test_dictionary_check_does_not_fire_on_incidental_words():
    """A random key containing a short word must not be rejected.

    Random base64/hex contains fragments like "face", "dead", "beta" by chance.
    The dictionary rule requires BOTH a word count and a coverage fraction
    specifically so these do not trip it — if it fired on one incidental word,
    it would be a false-positive generator.
    """
    # 'face' and 'beta' embedded in otherwise random material.
    for value in (
        "face" + secrets.token_hex(30),
        secrets.token_hex(20) + "beta" + secrets.token_hex(10),
        "dead" + secrets.token_urlsafe(40),
    ):
        assert assess_hmac_secret(value) == [], (
            "an incidental dictionary word caused a rejection — the coverage "
            "threshold is too low"
        )


def test_known_digest_rejection_is_case_insensitive():
    """A common-password hash must be caught in either case.

    Uppercasing a digest is a trivial evasion and must not work.
    """
    lower = "5f4dcc3b5aa765d61d8327deb882cf99"          # md5('password')
    assert assess_hmac_secret(lower)
    assert assess_hmac_secret(lower.upper())


def test_a_random_hash_length_key_is_still_accepted():
    """The digest check must match KNOWN digests only, not all hash-shaped keys.

    `secrets.token_hex(32)` is indistinguishable in shape from a sha256 digest.
    Rejecting it would break the most common way to generate a key.
    """
    for _ in range(200):
        assert assess_hmac_secret(secrets.token_hex(32)) == []


def test_require_strong_raises_weak_key_error_with_guidance():
    """The enforcement wrapper raises a typed error containing the fix."""
    with pytest.raises(WeakKeyError) as exc:
        require_strong_hmac_secret(
            "short", label="SESSION_JWT_SECRET", env_var="SESSION_JWT_SECRET"
        )
    msg = str(exc.value)
    assert "SESSION_JWT_SECRET" in msg
    assert "CVE-2025-45768" in msg
    # An error that says "no" without saying "here is the yes" gets worked
    # around rather than fixed.
    assert "secrets.token_urlsafe" in msg


def test_require_strong_accepts_a_good_key():
    require_strong_hmac_secret(
        secrets.token_urlsafe(48), label="X", env_var="X"
    )  # must not raise


def test_error_message_never_echoes_the_secret():
    """Rejection reasons must not contain the offending value.

    These strings land in logs and (for `npci_jwt_secret`) in an HTTP 400 body.
    Echoing the key would turn a strength check into a credential-disclosure
    bug — the classic way a secret reaches a log aggregator.
    """
    # Built rather than written out: a literal here is the shape Checkmarx
    # reports as "Use Of Hardcoded Password" (see
    # tests/test_no_hardcoded_secret_literals.py). The value still has to be
    # REJECTED for the assertion to mean anything, so it embeds a real token
    # from the module's own placeholder list; `marker` is the random, unique
    # part that must never be reflected back in a reason string.
    marker = secrets.token_urlsafe(12)
    secret = f"{_PLACEHOLDER_TOKENS[0]}-{marker}-that-should-never-be-echoed"
    reasons = assess_hmac_secret(secret, label="MY_KEY")
    assert reasons, "expected this placeholder-bearing value to be rejected"
    for r in reasons:
        assert secret not in r
        assert marker not in r


def test_generation_hint_names_the_env_var():
    hint = generation_hint("NPCI_JWT_SECRET")
    assert "NPCI_JWT_SECRET" in hint
    assert "token_urlsafe" in hint


def test_documented_generation_command_produces_a_compliant_key():
    """Our own advice must actually work — 500 rounds of the exact command.

    `generation_hint()` tells operators to run `secrets.token_urlsafe(48)`. If
    that ever produced a rejected key, the control would be self-contradicting.
    """
    for _ in range(500):
        assert assess_hmac_secret(secrets.token_urlsafe(48)) == []


def test_label_appears_in_every_reason():
    """Reasons name the setting, so an operator with several secrets configured
    knows which one to regenerate."""
    for r in assess_hmac_secret("abc", label="NPCI_HMAC_SECRET"):
        assert "NPCI_HMAC_SECRET" in r


def test_all_reasons_are_reported_not_just_the_first():
    """A value violating several rules should surface all of them at once.

    Fixing one problem at a time across repeated rejections is how operators end
    up picking a slightly longer bad password.
    """
    reasons = assess_hmac_secret("secret", label="X")
    assert len(reasons) >= 2, reasons


# ── the padding-run threshold, pinned to the measurement that set it ─────────


def test_padding_run_threshold_exceeds_what_real_csprng_output_produces():
    """The run threshold must sit ABOVE the longest run legitimate keys contain.

    This is the regression test for a defect that presented as flakiness. The
    threshold was 8, but `token_hex(32)` produces runs of 9 — measured over
    300,000 samples per format:

        base64(48)     5        uuid4          6
        base32         5        numeric 32/40  7
        token_hex(16)  7        token_hex(32)  9

    A threshold at or below 9 is therefore not unlucky, it is certain to reject
    valid keys eventually. The suite failed roughly 1 run in 10, always on the
    same case, which reads as an unreliable test rather than a real rule — the
    most expensive kind of wrong, because the usual response is to retry the
    build.

    Sampling here would reintroduce the flake this test exists to prevent, so it
    asserts the invariant directly against the recorded maxima instead.
    """
    observed_max_run_in_legitimate_keys = 9
    assert MAX_ABSOLUTE_RUN > observed_max_run_in_legitimate_keys, (
        f"MAX_ABSOLUTE_RUN={MAX_ABSOLUTE_RUN} is not above the longest run "
        f"({observed_max_run_in_legitimate_keys}) observed in legitimate CSPRNG "
        f"output. It will reject valid NPCI-issued keys at a low, intermittent "
        f"rate. Re-measure with backend/scripts/calibrate_key_strength.py "
        f"before lowering this."
    )
    assert MIN_PADDING_RUN > 7, (
        "the proportional arm must not fire below 8 either, or short numeric "
        "keys (max observed run 7) are rejected by the other branch"
    )


@pytest.mark.parametrize(
    "value",
    [
        "a" * 32,
        "secret" + "0" * 30,
        "npci" + "x" * 36,
        "pad" + "-" * 40,
        "A" * 64,
    ],
)
def test_real_padding_is_still_caught_after_widening_the_threshold(value):
    """Widening the bar to 12 must not cost detection.

    It does not, and the margin is large: every value in the weak corpus pads
    with a run of 30 or more. Nobody pads a secret with four characters — they
    hold down a key until the length check stops complaining.
    """
    assert _longest_run(value) >= 30, "test corpus no longer models real padding"
    reasons = assess_hmac_secret(value)
    assert any("repeated character" in r for r in reasons), (
        f"padding not detected in {value[:20]!r}...: {reasons}"
    )
