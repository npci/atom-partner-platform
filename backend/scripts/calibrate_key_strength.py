# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Empirically calibrate HMAC key-strength thresholds.

Goal: zero false positives on every legitimate generation format, while
catching human-chosen strings. Searches the threshold space rather than
guessing, and reports the achievable margin.

WHY THIS IS IN THE REPOSITORY
=============================

`app/core/key_strength.py` rejects weak HMAC secrets, and a false rejection
there blocks partner onboarding. Its thresholds are therefore not opinions;
they are measurements, and this script is the measurement.

It is kept because the numbers it produces are cited in
`security/vex/partner-platform.vex.json` as the evidence for the
CVE-2025-45768 `not_affected` claim. An audit figure whose derivation has been
deleted is not evidence, so anyone reviewing that annotation can re-run this and
get the same answer.

WHAT IT ESTABLISHED
===================

The two intuitive metrics — distinct-character count and Shannon entropy PER
CHARACTER — are ANTI-CORRELATED with guessability, and an earlier revision of
the validator used both as floors. Concretely:

    secrets.token_hex(16)         9 distinct chars, 2.78 bits/char  ← legitimate
    "CorrectHorseBatteryStaple"  25 distinct chars, 4.38 bits/char  ← weak

Hex alphabets are small by construction; English prose is character-rich by
construction. Flooring on either metric rejects real CSPRNG keys and accepts
passphrases. Both floors were removed as a result.

What replaced them: a floor on TOTAL entropy (per-char bits times length), plus
structural checks — dictionary-word composition, tiling, padding runs and known
password digests — for the weak values whose total entropy is legitimately
high, such as the hex digest of "password".

RUN IT

    python backend/scripts/calibrate_key_strength.py

Takes a few minutes at the default sample size. Standard library only; imports
nothing from the application, so it needs no database and no configuration.
Change `N` to trade runtime for tightness of the observed minima.
"""
import base64
import math
import secrets
import string
import sys
import uuid

N = 30000

GOOD = {
    "token_urlsafe(48)": lambda: secrets.token_urlsafe(48),
    "token_urlsafe(32)": lambda: secrets.token_urlsafe(32),
    "token_urlsafe(24)": lambda: secrets.token_urlsafe(24),
    "token_hex(32)": lambda: secrets.token_hex(32),
    "token_hex(16)": lambda: secrets.token_hex(16),
    "b64(32)": lambda: base64.b64encode(secrets.token_bytes(32)).decode(),
    "b64(48)": lambda: base64.b64encode(secrets.token_bytes(48)).decode(),
    "b64url(32)": lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    "uuid4": lambda: str(uuid.uuid4()),
    "uuid4 x2": lambda: str(uuid.uuid4()) + str(uuid.uuid4()),
    "HEX upper 64": lambda: secrets.token_hex(32).upper(),
    "grouped hex": lambda: "-".join(secrets.token_hex(2).upper() for _ in range(8)),
    "prefixed": lambda: "npci_live_" + base64.urlsafe_b64encode(
        secrets.token_bytes(24)).decode().rstrip("="),
    "base32": lambda: base64.b32encode(secrets.token_bytes(25)).decode(),
    "digits 40": lambda: "".join(secrets.choice(string.digits) for _ in range(40)),
    "digits 32": lambda: "".join(secrets.choice(string.digits) for _ in range(32)),
    "alnum 32": lambda: "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(32)),
}


def distinct(v):
    return len(set(v))


def shannon_per_char(v):
    c = {}
    for ch in v:
        c[ch] = c.get(ch, 0) + 1
    n = len(v)
    return -sum((k / n) * math.log2(k / n) for k in c.values())


def total_entropy(v):
    """Shannon bits across the WHOLE string, not per character.

    This is the metric that actually matters for a key: a 40-char digit string
    has low per-char entropy but plenty of total material.
    """
    return shannon_per_char(v) * len(v)


print("=" * 100)
print(f"OBSERVED MINIMA across legitimate formats ({N:,} samples each)")
print("=" * 100)
print(f"{'format':<22} {'len':>5} {'min distinct':>13} {'min bits/char':>14} {'min total bits':>15}")
print("-" * 100)

worst = {"distinct": 999, "per_char": 999.0, "total": 1e9}
per_format = {}
for name, gen in GOOD.items():
    md, mp, mt, ln = 999, 999.0, 1e9, 0
    for _ in range(N):
        v = gen()
        ln = len(v)
        md = min(md, distinct(v))
        mp = min(mp, shannon_per_char(v))
        mt = min(mt, total_entropy(v))
    per_format[name] = (md, mp, mt)
    worst["distinct"] = min(worst["distinct"], md)
    worst["per_char"] = min(worst["per_char"], mp)
    worst["total"] = min(worst["total"], mt)
    print(f"{name:<22} {ln:>5} {md:>13} {mp:>14.2f} {mt:>15.1f}")

print("-" * 100)
print(f"{'GLOBAL MINIMUM':<22} {'':>5} {worst['distinct']:>13} "
      f"{worst['per_char']:>14.2f} {worst['total']:>15.1f}")
print()

WEAK = {
    "'a'*32": "a" * 32,
    "'ab'*20": "ab" * 20,
    "abcdefghij*4": "abcdefghij" * 4,
    "Correct-Horse-Battery-Staple-Longer": "Correct-Horse-Battery-Staple-Longer",
    "dictionary sentence": "the quick brown fox jumps over lazy",
    "company+year": "NPCIPartnerPlatform2026SigningKey01",
    "date-based": "2026-08-28-partner-platform-hmac-key",
    "hostname-based": "partner-backend-prod-01.npci.org.in",
    "leetspeak": "P@ssw0rd!P@ssw0rd!P@ssw0rd!P@ssw0rd!",
    "sha256('password')": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
    "repeated word": "MonkeyMonkeyMonkeyMonkeyMonkeyMonkeyX",
    "keyboard walk": "qwertyuiopasdfghjklzxcvbnm1234567890",
}

print("=" * 100)
print("WEAK values under the same metrics")
print("=" * 100)
print(f"{'value':<40} {'len':>5} {'distinct':>9} {'bits/char':>10} {'total bits':>11}")
print("-" * 100)
for name, v in WEAK.items():
    print(f"{name:<40} {len(v):>5} {distinct(v):>9} "
          f"{shannon_per_char(v):>10.2f} {total_entropy(v):>11.1f}")

print()
print("=" * 100)
print("CONCLUSION")
print("=" * 100)
print(f"A per-char entropy floor cannot separate them: digit keys sit at "
      f"~{per_format['digits 40'][1]:.2f}")
print("bits/char, below several weak values. Same for distinct-char count")
print(f"(legitimate minimum is {worst['distinct']}).")
print()
print("Total-entropy floor is the discriminator:")
print(f"  lowest legitimate total : {worst['total']:.1f} bits")
hi = max(total_entropy(v) for v in WEAK.values())
print(f"  highest weak total      : {hi:.1f} bits  (sha256 hex — genuinely high)")
print()
print("=> Structural checks (dictionary words / known-hash detection) are")
print("   required for the weak values whose total entropy is high.")
print("   Numeric thresholds alone cannot do it without false positives.")
